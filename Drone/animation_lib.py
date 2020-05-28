import os
import time
import csv
import copy
import numpy
import rospy
import logging
import threading

try:
    from FlightLib import FlightLib
except ImportError:
    print("Can't import FlightLib")
try:
    from FlightLib import LedLib
except ImportError:
    print("Can't import LedLib")

logger = logging.getLogger(__name__)

interrupt_event = threading.Event()

def moving(f1, f2, delta, x=True, y=True, z=True):
    return ((abs(f1.x - f2.x) > delta) and x
        or  (abs(f1.y - f2.y) > delta) and y
        or  (abs(f1.z - f2.z) > delta) and z)

def get_numbers(frames):
    numbers = []
    if frames:
        for frame in frames:
            numbers.append(frame.number)
    return numbers

class Frame(object):
    params_dict = {
        "number": None,
        "x": None,
        "y": None,
        "z": None,
        "yaw": None,
        "red": None,
        "green": None,
        "blue": None,
        "delay": None,
    }
    def __init__(self, csv_row=None, delay=None):
        for key, value in self.params_dict.items():
            setattr(self, key, value)
        if csv_row:
            self.load_csv_row(csv_row)
        if delay:
            self.delay = delay

    def load_csv_row(self, csv_row):
        number, x, y, z, yaw, red, green, blue = csv_row
        self.number = int(number)
        self.x = float(x)
        self.y = float(y)
        self.z = float(z)
        self.yaw = float(yaw)
        self.red = int(red)
        self.green = int(green)
        self.blue = int(blue)

    def get_pos(self):
        if None in [self.x, self.y, self.z]:
            return []
        else:
            return [self.x, self.y, self.z]

    def get_color(self):
        if None in [self.red, self.green, self.blue]:
            return []
        else:
            return [self.red, self.green, self.blue]

    def pose_is_valid(self):
        return self.get_pos() and (self.yaw is not None)

class Animation(object):
    def __init__(self, config=None, filepath="animation.csv"):
        self.id = None
        self.static_begin_time = 0
        self.takeoff_time = 0
        self.original_frames = None
        self.static_begin_frames = None
        self.takeoff_frames = None
        self.route_frames = None
        self.land_frames = None
        self.static_end_frames = None
        self.output_frames = None
        self.output_frames_min_z = None
        self.filepath = filepath
        if config is not None:
            self.update_frames(config, filepath)

    def load(self, filepath="animation.csv", delay=0.1):
        self.original_frames = []
        self.corrected_frames = []
        self.filepath = filepath
        try:
            animation_file = open(filepath)
        except IOError:
            logger.debug("File {} can't be opened".format(filepath))
            self.id = "No animation"
        else:
            with animation_file:
                current_frame_delay = delay
                csv_reader = csv.reader(
                    animation_file, delimiter=',', quotechar='|'
                )
                row_0 = csv_reader.next()
                if len(row_0) == 1:
                    self.id = row_0[0]
                    logger.debug("Got animation_id: {}".format(self.id))
                elif len(row_0) == 2:
                    current_frame_delay = float(row_0[1])
                    logger.debug("Got new frame delay: {}".format(current_frame_delay))
                else:
                    logger.debug("No animation id in file")
                    try:
                        frame = Frame(row_0, current_frame_delay)
                    except ValueError as e:
                        logger.error("Can't parse row in csv file. {}".format(e))
                        return
                    else:
                        self.original_frames.append(frame)
                for row in csv_reader:
                    if len(row) == 2:
                        current_frame_delay = float(row[1])
                        logger.debug("Got new frame delay: {}".format(current_frame_delay))
                    else:
                        try:
                            frame = Frame(row, current_frame_delay)
                        except ValueError as e:
                            logger.error("Can't parse row in csv file. {}".format(e))
                            return
                        else:
                            self.original_frames.append(frame)
        self.split_animation()

    '''
    Split animation into 5 parts: static_begin, takeoff, route, land, static_end
        * static_begin and static_end are chains of frames in the beginning and the end of animation,
            where the drone doesn't move
        * takeoff and land are chains of frames after and before static frames of animation,
            where the drone doesn't move in xy plane, and it's z coordinate only increases or decreases, respectively.
        * route is the rest of the animation
    Count static_begin_time and takeoff_time
    '''
    def split_animation(self, move_delta=0.01):
        self.static_begin_frames = []
        self.takeoff_frames = []
        self.route_frames = []
        self.land_frames = []
        self.static_end_frames = []
        self.static_begin_time = 0
        self.takeoff_time = 0
        if len(self.original_frames) == 0:
            return
        frames = copy.deepcopy(self.original_frames)
        i = 0 # Moving index from the beginning
        # Select static begin frames
        while i < len(frames) - 1:
            self.static_begin_time += frames[i].delay
            if moving(frames[i], frames[i+1], move_delta):
                break
            i += 1
        if i > 0:
            self.static_begin_frames = frames[:i+1]
            frames = frames[i+1:]
            i = 0
        else:
            self.static_begin_time = 0
        # Select takeoff frames
        while i < len(frames) - 1:
            self.takeoff_time += frames[i].delay
            if moving(frames[i], frames[i+1], move_delta, z = False) or (frames[i+1].z - frames[i].z <= 0):
                break
            i += 1
        if i > 0:
            self.takeoff_frames = frames[:i+1]
            frames = frames[i+1:]
        else:
            self.takeoff_time = 0
        i = len(frames) - 1 # Moving index from the end
        # Select static end frames
        while i >= 0:
            if moving(frames[i], frames[i-1], move_delta):
                break
            i -= 1
        if i < len(frames) - 1:
            self.static_end_frames = frames[i+1:]
            frames = frames[:i+1]
            i = len(frames) - 1
        # Select land frames
        while i >= 0:
            if moving(frames[i], frames[i-1], move_delta, z = False) or (frames[i-1].z - frames[i].z <= 0):
                break
            i -= 1
        if i < len(frames) - 1:
            self.land_frames = frames[i+1:]
            frames = frames[:i+1]
        # Get route frames
        self.route_frames = frames

    def make_output_frames(self, static_begin, takeoff, route, land, static_end):
        self.output_frames = []
        self.output_frames_min_z = None
        if static_begin:
            self.output_frames += self.static_begin_frames
        if takeoff:
            self.output_frames += self.takeoff_frames
        if route:
            self.output_frames += self.route_frames
        if land:
            self.output_frames += self.land_frames
        if static_end:
            self.output_frames += self.static_end_frames
        if self.output_frames:
            self.output_frames_min_z = min(self.output_frames, key = lambda p: p.z).z

    def update_frames(self, config, filepath):
        self.__init__()
        self.load(filepath, config.animation_frame_delay)
        self.make_output_frames(config.animation_output_static_begin,
                                    config.animation_output_takeoff,
                                    config.animation_output_route,
                                    config.animation_output_land,
                                    config.animation_output_static_end)

    def get_scaled_output(self, ratio = (1,1,1), offset = (0,0,0)):
        x0, y0, z0 = offset
        x_ratio, y_ratio, z_ratio = ratio
        scaled_frames = copy.deepcopy(self.output_frames)
        for frame in scaled_frames:
            frame.x = x_ratio*frame.x + x0
            frame.y = y_ratio*frame.y + y0
            frame.z = z_ratio*frame.z + z0
        return scaled_frames

    def get_scaled_output_min_z(self, ratio = (1,1,1), offset = (0,0,0)):
        x0, y0, z0 = offset
        x_ratio, y_ratio, z_ratio = ratio
        return self.output_frames_min_z*z_ratio + z0

    def get_start_point(self, ratio = (1,1,1), offset = (0,0,0)):
        x0, y0, z0 = offset
        x_ratio, y_ratio, z_ratio = ratio
        first_frame = self.output_frames[0]
        x = x_ratio*first_frame.x + x0
        y = y_ratio*first_frame.y + y0
        z = z_ratio*first_frame.z + z0
        return x, y, z

    def get_start_action(self, start_action, current_height, takeoff_level):
        if start_action is 'auto':
            if current_height > takeoff_level:
                return 'takeoff'
            else:
                return 'play'
        elif start_action in ('takeoff', 'play'):
            return start_action
        else:
            return 'error'

    def check_ground(self, ground_level = 0, ratio = (1,1,1), offset = (0,0,0)):
        return ground_level <= self.get_scaled_output_min_z(ratio, offset)

    # Need for tests
    def save_corrected_animation(self):
        name, ext = os.path.splitext(self.filepath)
        filepath = name + '_corrected' + ext
        with open(filepath, mode='w+') as corrected_animation:
            csv_writer = csv.writer(corrected_animation, delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)
            for frame in self.corrected_frames:
                csv_writer.writerow([frame.number, frame.x, frame.y, frame.z, frame.red, frame.green, frame.blue, frame.delay])

try:
    def execute_frame(frame, frame_id='aruco_map', use_leds=True,
                    flight_func=FlightLib.navto, auto_arm=False, flight_kwargs=None, interrupter=interrupt_event):
        if flight_kwargs is None:
            flight_kwargs = {}
        if frame.pose_is_valid():
            flight_func(x=frame.x, y=frame.y, z=frame.z, yaw=frame.yaw, frame_id=frame_id, auto_arm=auto_arm, interrupter=interrupt_event, **flight_kwargs)
        else:
            logger.debug("Frame pose is not valid for flying")
        if use_leds:
            if frame.get_color:
                LedLib.fill(*color)

    def takeoff(z=1.5, safe_takeoff=True, frame_id='map', timeout=5.0, use_leds=True,
                interrupter=interrupt_event):
        if use_leds:
            LedLib.wipe_to(255, 0, 0, interrupter=interrupter)
        result = FlightLib.takeoff(height=z, timeout_takeoff=timeout, frame_id=frame_id,
                                emergency_land=safe_takeoff, interrupter=interrupter)
        if result == 'not armed' or result == 'timeout':
            raise Exception('STOP')  # Raise exception to clear task_manager if copter can't arm
        if use_leds:
            LedLib.blink(0, 255, 0, wait=50, interrupter=interrupter)


    def land(z=1.5, descend=False, timeout=5.0, frame_id='aruco_map', use_leds=True,
            interrupter=interrupt_event):
        if use_leds:
            LedLib.blink(255, 0, 0, interrupter=interrupter)
        FlightLib.land(z=z, descend=descend, timeout_land=timeout, frame_id_land=frame_id, interrupter=interrupter)
        if use_leds:
            LedLib.off()

except NameError:
    print("Can't create flying functions")
