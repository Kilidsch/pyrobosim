""" Utilities for displaying a pyrobosim world in a figure canvas. """

import adjustText
import numpy as np
import time
import threading
import warnings
from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
from matplotlib.pyplot import Circle
from matplotlib.transforms import Affine2D
from PySide6.QtCore import QRunnable, QThreadPool, QTimer


class NavRunner(QRunnable):
    """
    Helper class that wraps navigation execution in a QThread.
    """

    def __init__(self, canvas, robot, goal, path):
        """
        Creates a navigation execution thread.

        :param canvas: A world canvas object linked to this thread.
        :type canvas: :class:`pyrobosim.gui.world_canvas.WorldCanvas`
        :param robot: Robot instance or name to execute action.
        :type robot: :class:`pyrobosim.core.robot.Robot` or str
        :param goal: Name of goal location (resolved by the world model).
        :type goal: str
        :param path: Path to goal location, defaults to None.
        :type path: :class:`pyrobosim.utils.motion.Path`, optional
        """
        super(NavRunner, self).__init__()
        self.canvas = canvas
        self.robot = robot
        self.goal = goal
        self.path = path

    def run(self):
        """Runs the navigation execution thread."""
        robot = self.robot
        world = self.canvas.world
        path = self.path

        if isinstance(robot, str):
            robot = world.get_robot_by_name(robot)
        if robot is None:
            warnings.warn("No robot found.")
            return
        if robot.path_planner is None:
            warnings.warn(f"No path planner attached to robot {robot.name}.")
            return

        # Find a path, or use an existing one, and start the navigation thread.
        goal_node = world.graph_node_from_entity(self.goal, robot=robot)
        if not path or path.num_poses < 2:
            path = robot.plan_path(robot.get_pose(), goal_node.pose)
        if path.num_poses == 0:
            warnings.warn(f"Failed to plan a path.")
            robot.executing_nav = False
            robot.last_nav_successful = False
            return

        self.canvas.show_planner_and_path(robot=robot, path=path)
        robot.follow_path(
            path,
            target_location=goal_node.parent,
            realtime_factor=self.canvas.realtime_factor,
            blocking=False,
        )

        # Sleep while the robot is executing the action.
        while robot.executing_nav:
            time.sleep(0.1)

        self.canvas.show_world_state(robot=robot)
        world.gui.update_button_state()


class WorldCanvas(FigureCanvasQTAgg):
    """
    Canvas for rendering a pyrobosim world as a matplotlib figure in an
    application.
    """

    # Visualization constants
    object_zorder = 3
    """ zorder for object visualization. """
    robot_zorder = 3
    """ zorder for robot visualization. """
    robot_dir_line_factor = 3.0
    """ Multiplier of robot radius for plotting robot orientation lines. """

    draw_lock = threading.RLock()
    """ Lock for drawing on the canvas in a thread-safe manner. """

    def __init__(
        self,
        main_window,
        world,
        show=True,
        dpi=100,
        animation_dt=0.1,
        realtime_factor=1.0,
    ):
        """
        Creates an instance of a pyrobosim figure canvas.

        :param main_window: The main window object, needed for bookkeeping.
        :type main_window: :class:`pyrobosim.gui.main.PyRoboSimMainWindow`
        :param world: World object to attach.
        :type world: :class:`pyrobosim.core.world.World`
        :param show: If true (default), shows the GUI. Otherwise runs headless for testing.
        :type show: bool, optional
        :param dpi: DPI for the figure.
        :type dpi: int
        :param animation_dt: Time step for animations (seconds).
        :type animation_dt: float
        :param realtime_factor: Real-time multiplication factor for animation (1.0 is real-time).
        :type realtime_factor: float
        """
        self.fig = Figure(dpi=dpi, tight_layout=True)
        self.axes = self.fig.add_subplot(111)
        super(WorldCanvas, self).__init__(self.fig)

        self.main_window = main_window
        self.world = world

        self.displayed_path = None
        self.displayed_path_start = None
        self.displayed_path_goal = None

        # Display/animation properties
        self.animation_dt = animation_dt
        self.realtime_factor = realtime_factor

        self.robot_bodies = []
        self.robot_dirs = []
        self.robot_lengths = []
        self.obj_patches = []
        self.obj_texts = []
        self.hallway_patches = []
        self.path_planner_artists = {"graph": [], "path": []}

        # Debug displays (TODO: Should be available from GUI).
        self.show_collision_polygons = False

        # Thread pool for managing long-running tasks in separate threads.
        self.thread_pool = QThreadPool()

        # Start timer for animating robot navigation state.
        if show:
            sleep_time_msec = int(1000.0 * self.animation_dt / self.realtime_factor)
            self.nav_animator = QTimer()
            self.nav_animator.timeout.connect(self.nav_animation_callback)
            self.nav_animator.start(sleep_time_msec)

    def show_robots(self):
        """Draws robots as circles with heading lines for visualization."""
        with self.draw_lock:
            n_robots = len(self.world.robots)
            for body in self.robot_bodies:
                body.remove()
            for dir in self.robot_dirs:
                dir.remove()
            self.robot_bodies = n_robots * [None]
            self.robot_dirs = n_robots * [None]
            self.robot_lengths = n_robots * [None]

            for i, robot in enumerate(self.world.robots):
                p = robot.get_pose()
                self.robot_bodies[i] = Circle(
                    (p.x, p.y),
                    radius=robot.radius,
                    edgecolor=robot.color,
                    fill=False,
                    linewidth=2,
                    zorder=self.robot_zorder,
                )
                self.axes.add_patch(self.robot_bodies[i])

                robot_length = self.robot_dir_line_factor * robot.radius
                (self.robot_dirs[i],) = self.axes.plot(
                    p.x + np.array([0, robot_length * np.cos(p.get_yaw())]),
                    p.y + np.array([0, robot_length * np.sin(p.get_yaw())]),
                    linestyle="-",
                    color=robot.color,
                    linewidth=2,
                    zorder=self.robot_zorder,
                )
                self.robot_lengths[i] = robot_length

                x = p.x
                y = p.y - 2.0 * robot.radius
                robot.viz_text = self.axes.text(
                    x,
                    y,
                    robot.name,
                    clip_on=True,
                    color=robot.color,
                    horizontalalignment="center",
                    verticalalignment="top",
                    fontsize=10,
                )
            self.robot_texts = [r.viz_text for r in (self.world.robots)]

    def show_hallways(self):
        """Draws hallways in the world."""
        with self.draw_lock:
            for hallway in self.hallway_patches:
                hallway.remove()

            self.hallway_patches = [h.viz_patch for h in self.world.hallways]

            for h in self.world.hallways:
                self.axes.add_patch(h.viz_patch)
                if not h.is_open:
                    closed_patch = h.get_closed_patch()
                    self.axes.add_patch(closed_patch)
                    self.hallway_patches.append(closed_patch)
                elif self.show_collision_polygons:
                    coll_patch = h.get_collision_patch()
                    self.axes.add_patch(coll_patch)
                    self.hallway_patches.append(coll_patch)

    def show_objects(self):
        """Draws objects and their associated texts."""
        with self.draw_lock:
            for obj_patch in self.obj_patches:
                obj_patch.remove()
            for obj_text in self.obj_texts:
                obj_text.remove()
            self.obj_patches = []
            self.obj_texts = []

            robot = self.main_window.get_current_robot()
            if robot:
                known_objects = robot.get_known_objects()
            else:
                known_objects = self.world.objects

            for obj in known_objects:
                self.axes.add_patch(obj.viz_patch)
                xmin, ymin, xmax, ymax = obj.polygon.bounds
                x = obj.pose.x + 1.0 * (xmax - xmin)
                y = obj.pose.y + 1.0 * (ymax - ymin)
                obj.viz_text = self.axes.text(
                    x, y, obj.name, clip_on=True, color=obj.viz_color, fontsize=8
                )
            self.obj_patches = [o.viz_patch for o in known_objects]
            self.obj_texts = [o.viz_text for o in known_objects]

            # Adjust the text to try avoid collisions
            adjustText.adjust_text(
                self.obj_texts, iter_lim=100, objects=self.obj_patches, ax=self.axes
            )

    def show(self):
        """
        Displays all entities in the world (robots, rooms, objects, etc.).
        """
        # Rooms and hallways
        for r in self.world.rooms:
            self.axes.add_patch(r.viz_patch)
            self.axes.text(
                r.centroid[0],
                r.centroid[1],
                r.name,
                color=r.viz_color,
                fontsize=12,
                ha="center",
                va="top",
                clip_on=True,
            )
            if self.show_collision_polygons:
                self.axes.add_patch(r.get_collision_patch())
        self.show_hallways()

        # Locations
        for loc in self.world.locations:
            self.axes.add_patch(loc.viz_patch)
            self.axes.text(
                loc.pose.x,
                loc.pose.y,
                loc.name,
                color=loc.viz_color,
                fontsize=10,
                ha="center",
                va="top",
                clip_on=True,
            )
            for spawn in loc.children:
                self.axes.add_patch(spawn.viz_patch)

        # Objects
        self.show_objects()

        # Robots, along with their paths and planner graphs
        self.show_robots()
        if len(self.world.robots) > 0:
            self.show_planner_and_path(robot=self.world.robots[0])

        self.axes.autoscale()
        self.axes.axis("equal")

    def draw_and_sleep(self):
        """Redraws the figure and waits a small amount of time."""
        with self.draw_lock:
            self.fig.canvas.draw()
            self.fig.canvas.flush_events()
            time.sleep(0.01)

    def show_planner_and_path(self, robot=None, path=None):
        """
        Plot the path planner and latest path, if specified.
        This planner could be global (property of the world)
        or local (property of the robot).

        :param robot: If set to a Robot instance, uses that robot for display.
        :type robot: :class:`pyrobosim.core.robot.Robot`, optional
        :param path: Path to goal location, defaults to None.
        :type path: :class:`pyrobosim.utils.motion.Path`, optional
        """
        # Since removing artists while drawing can cause issues,
        # this function should also lock drawing.
        with self.draw_lock:
            if not robot:
                warnings.warn("No robot found")
            else:
                color = robot.color if robot is not None else "m"
                if robot.path_planner:
                    path_planner_artists = robot.path_planner.plot(
                        self.axes, path=path, path_color=color
                    )

                    for artist in self.path_planner_artists["graph"]:
                        artist.remove()
                    self.path_planner_artists["graph"] = path_planner_artists.get(
                        "graph", []
                    )

                    for artist in self.path_planner_artists["path"]:
                        artist.remove()
                    self.path_planner_artists["path"] = path_planner_artists.get(
                        "path", []
                    )

    def nav_animation_callback(self):
        """Timer callback function to animate navigating robots."""
        if not self.main_window.isVisible():
            return

        world = self.world
        # Check if any robot is currently navigating.
        nav_status = [robot.is_moving() for robot in world.robots]
        if any(nav_status):
            self.update_robots_plot()

            # Show the state of the currently selected robot
            cur_robot = world.gui.get_current_robot()
            if cur_robot is not None and cur_robot.is_moving():
                self.show_world_state(cur_robot, navigating=True)
                world.gui.set_buttons_during_action(False)

            self.draw_and_sleep()

    def update_robots_plot(self):
        """Updates the robot visualization graphics objects."""
        with self.draw_lock:
            if len(self.world.robots) != len(self.robot_bodies):
                self.show_robots()
            for i, robot in enumerate(self.world.robots):
                p = robot.get_pose()
                self.robot_bodies[i].center = p.x, p.y
                self.robot_dirs[i].set_xdata(
                    p.x + np.array([0, self.robot_lengths[i] * np.cos(p.get_yaw())])
                )
                self.robot_dirs[i].set_ydata(
                    p.y + np.array([0, self.robot_lengths[i] * np.sin(p.get_yaw())])
                )
                robot.viz_text.set_position((p.x, p.y - 2.0 * robot.radius))
                self.update_object_plot(robot.manipulated_object)

    def show_world_state(self, robot=None, navigating=False):
        """
        Shows the world state in the figure title.

        :param robot: If set to a Robot instance, uses that robot for showing state.
        :type robot: :class:`pyrobosim.core.robot.Robot`, optional
        :param navigating: Flag that indicates that the robot is moving so we
            should continuously update the title containing the robot location.
        :type navigating: bool, optional
        """
        if robot is not None:
            title_bits = []
            if navigating:
                robot_loc = self.world.get_location_from_pose(robot.get_pose())
                if robot_loc is not None:
                    title_bits.append(f"Location: {robot_loc.name}")
            elif robot.location is not None:
                if isinstance(robot.location, str):
                    robot_loc = robot.location
                else:
                    robot_loc = robot.location.name
                title_bits.append(f"Location: {robot_loc}")
            if robot.manipulated_object is not None:
                title_bits.append(f"Holding: {robot.manipulated_object.name}")
            title_str = f"[{robot.name}] " + ", ".join(title_bits)
            self.axes.set_title(title_str)

    def update_object_plot(self, obj):
        """
        Updates an object visualization based on its pose.

        :param obj: pyrobosim object to update.
        :type obj: class:`pyrobosim.objects.Object`
        """
        if obj is None:
            return

        tf = (
            Affine2D()
            .translate(-obj.centroid[0], -obj.centroid[1])
            .rotate(obj.pose.get_yaw())
            .translate(obj.pose.x, obj.pose.y)
        )
        obj.viz_patch.set_transform(tf + self.axes.transData)

        xmin, ymin, xmax, ymax = obj.polygon.bounds
        x = obj.pose.x + 1.0 * (xmax - xmin)
        y = obj.pose.y + 1.0 * (ymax - ymin)
        obj.viz_text.set_position((x, y))

    def navigate(self, robot, goal, path=None):
        """
        Starts a thread to navigate a robot to a goal.

        :param robot: Robot instance or name to execute action.
        :type robot: :class:`pyrobosim.core.robot.Robot` or str
        :param goal: Name of goal location (resolved by the world model).
        :type goal: str
        :param path: Path to goal location, defaults to None.
        :type path: :class:`pyrobosim.utils.motion.Path`, optional
        """
        nav_thread = NavRunner(self, robot, goal, path)
        self.thread_pool.start(nav_thread)

    def pick_object(self, robot, obj_name, grasp_pose=None):
        """
        Picks an object with a specified robot.

        :param robot: Robot instance to execute action.
        :type robot: :class:`pyrobosim.core.robot.Robot`
        :param obj_name: The name of the object.
        :type obj_name: str
        :param grasp_pose: A pose describing how to manipulate the object.
        :type grasp_pose: :class:`pyrobosim.utils.pose.Pose`, optional
        :return: True if picking succeeds, else False.
        :rtype: bool
        """
        if robot is None:
            return False

        success = robot.pick_object(obj_name, grasp_pose)
        if success:
            self.update_object_plot(robot.manipulated_object)
            self.show_world_state(robot)
            self.draw_and_sleep()
        return success

    def place_object(self, robot, pose=None):
        """
        Places an object at a specified robot's current location.

        :param robot: Robot instance to execute action.
        :type robot: :class:`pyrobosim.core.robot.Robot`
        :param pose: Optional placement pose, defaults to None.
        :type pose: :class:`pyrobosim.utils.pose.Pose`, optional
        :return: True if placing succeeds, else False.
        :rtype: bool
        """
        if robot is None:
            return False

        obj = robot.manipulated_object
        if obj is None:
            return
        self.obj_patches.remove(obj.viz_patch)
        obj.viz_patch.remove()
        success = robot.place_object(pose=pose)
        self.axes.add_patch(obj.viz_patch)
        self.obj_patches.append(obj.viz_patch)
        self.show_world_state(robot)
        self.draw_and_sleep()
        return success

    def detect_objects(self, robot, query=None):
        """
        Detects objects at the robot's current location.

        :param robot: Robot instance to execute action.
        :type robot: :class:`pyrobosim.core.robot.Robot`
        :param query: Query for object detection.
        :type query: str, optional
        :return: True if object detection succeeds, else False.
        :rtype: bool
        """
        if robot is None:
            return False

        success = robot.detect_objects(query)
        self.show_objects()
        self.draw_and_sleep()
        return success

    def open_location(self, robot):
        """
        Opens the robot's current location, if available.

        :param robot: Robot instance to execute action.
        :type robot: :class:`pyrobosim.core.robot.Robot`
        :return: True if opening the location succeeds, else False.
        :rtype: bool
        """
        if robot is None:
            return False

        return robot.open_location()

    def close_location(self, robot):
        """
        Closes the robot's current location, if available.

        :param robot: Robot instance to execute action.
        :type robot: :class:`pyrobosim.core.robot.Robot`
        :return: True if closing the location succeeds, else False.
        :rtype: bool
        """
        if robot is None:
            return False

        return robot.close_location()
