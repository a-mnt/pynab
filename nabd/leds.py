import abc
import time
import math
from enum import Enum, unique
from threading import Condition, Lock, Thread


@unique
class Led(Enum):
    BOTTOM = 4
    RIGHT = 3  # when looking at the rabbit
    CENTER = 2
    LEFT = 1
    NOSE = 0


class Leds(object, metaclass=abc.ABCMeta):
    """Interface for leds"""

    @abc.abstractmethod
    def set1(self, led, red, green, blue):
        raise NotImplementedError("Should have implemented")

    @abc.abstractmethod
    def pulse(self, led, red, green, blue):
        raise NotImplementedError("Should have implemented")

    @abc.abstractmethod
    def setall(self, red, green, blue):
        raise NotImplementedError("Should have implemented")

    def stop(self):
        pass


class LedsSoft(Leds, metaclass=abc.ABCMeta):
    """
    Base implementation with software pulsing.
    """

    PULSING_RATE = 0.05  # refresh every 50ms
    PULSING_STEPS = 40   # full cycle = 2*PULSING_STEPS*PULSING_RATE = 4s

    def __init__(self):
        self.condition = Condition()
        self.pending = []
        self.pulsing = {}
        self.pending_lock = Lock()
        self.start_time = None
        self.running = True
        self.thread = Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        with self.condition:
            while self.running:
                show = False
                with self.pending_lock:
                    for cmd, led, (r, g, b) in self.pending:
                        if cmd == "pulse":
                            self.do_set(led, 0, 0, 0)
                            show = True
                            if self.start_time is None:
                                self.start_time = time.time()
                            self.pulsing[led] = (r, g, b)
                        elif cmd == "set":
                            if led in self.pulsing:
                                del self.pulsing[led]
                            self.do_set(led, r, g, b)
                            show = True
                    self.pending = []

                next_pulse = None
                if self.pulsing:
                    now = time.time()
                    if self.start_time is None:
                        self.start_time = now
                    cycle_duration = self.PULSING_STEPS * self.PULSING_RATE * 2
                    t = (now - self.start_time) % cycle_duration / cycle_duration
                    factor = 0.5 * (1 - math.cos(2 * math.pi * t))

                    for led, (target_r, target_g, target_b) in self.pulsing.items():
                        new_r = int(target_r * factor)
                        new_g = int(target_g * factor)
                        new_b = int(target_b * factor)
                        self.do_set(led, new_r, new_g, new_b)
                        show = True

                    next_pulse = now + self.PULSING_RATE

                if show:
                    self.do_show()

                timeout = None
                if next_pulse is not None:
                    delta = next_pulse - time.time()
                    timeout = max(0, delta)

                self.condition.wait(timeout=timeout)

    def set1(self, led, red, green, blue):
        with self.pending_lock:
            self.pending.append(("set", led, (red, green, blue)))
        with self.condition:
            self.condition.notify()

    def pulse(self, led, red, green, blue):
        with self.pending_lock:
            self.pending.append(("pulse", led, (red, green, blue)))
        with self.condition:
            self.condition.notify()

    def setall(self, red, green, blue):
        with self.pending_lock:
            for led in list(Led):
                self.pending.append(("set", led, (red, green, blue)))
        with self.condition:
            self.condition.notify()

    def stop(self):
        with self.condition:
            self.running = False
            self.condition.notify()
        self.thread.join()

    @abc.abstractmethod
    def do_set(self, led, red, green, blue):
        """Actually set a led."""
        pass

    @abc.abstractmethod
    def do_show(self):
        """Show all leds at once."""
        pass
