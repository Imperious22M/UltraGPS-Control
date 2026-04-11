#!/usr/bin/env python3
"""
Test script for SettingsModule and live serial value monitoring.
Tests all getters and setters, verifying that changes persist to file.
Also connects to the UltraGPS server and displays live serial values.
"""

from SettingsModule import SettingsModule
from ControlModule import ControlModule
import copy
import argparse
import matplotlib.pyplot as plt
from collections import deque

PLOT_HISTORY = 100  # Number of data points to keep in the rolling plot


def test_top_level_properties():
    """Test top-level property getters and setters."""
    print("=" * 60)
    print("Testing top-level properties")
    print("=" * 60)

    settings = SettingsModule()

    # Store original values to restore later
    original_cal_state = settings.cal_state
    original_calibration_reads = settings.calibration_reads
    original_number_of_receivers = settings.number_of_receivers
    original_serial_port = settings.serial_port
    original_units = settings.units

    print(f"\n--- Reading properties ---")
    print(f"cal_state: {settings.cal_state}")
    print(f"calibration_reads: {settings.calibration_reads}")
    print(f"number_of_receivers: {settings.number_of_receivers}")
    print(f"serial_port: {settings.serial_port}")
    print(f"units: {settings.units}")

    print(f"\n--- Writing properties ---")

    # Test cal_state
    test_val = 99
    settings.cal_state = test_val
    assert settings.cal_state == test_val, f"cal_state setter failed: expected {test_val}, got {settings.cal_state}"
    print(f"cal_state = {test_val} ... OK")

    # Test calibration_reads
    test_val = 10
    settings.calibration_reads = test_val
    assert settings.calibration_reads == test_val, f"calibration_reads setter failed"
    print(f"calibration_reads = {test_val} ... OK")

    # Test number_of_receivers
    test_val = 8
    settings.number_of_receivers = test_val
    assert settings.number_of_receivers == test_val, f"number_of_receivers setter failed"
    print(f"number_of_receivers = {test_val} ... OK")

    # Test serial_port
    test_val = "/dev/ttyUSB0"
    settings.serial_port = test_val
    assert settings.serial_port == test_val, f"serial_port setter failed"
    print(f"serial_port = '{test_val}' ... OK")

    # Test units
    test_val = "inches"
    settings.units = test_val
    assert settings.units == test_val, f"units setter failed"
    print(f"units = '{test_val}' ... OK")

    print(f"\n--- Verifying persistence (reload from file) ---")
    settings.reload_config()
    assert settings.cal_state == 99, "cal_state not persisted"
    assert settings.calibration_reads == 10, "calibration_reads not persisted"
    assert settings.number_of_receivers == 8, "number_of_receivers not persisted"
    assert settings.serial_port == "/dev/ttyUSB0", "serial_port not persisted"
    assert settings.units == "inches", "units not persisted"
    print("All values persisted correctly after reload ... OK")

    # Restore original values
    print(f"\n--- Restoring original values ---")
    settings.cal_state = original_cal_state
    settings.calibration_reads = original_calibration_reads
    settings.number_of_receivers = original_number_of_receivers
    settings.serial_port = original_serial_port
    settings.units = original_units
    print("Original values restored ... OK")

    print("\nTop-level properties: ALL TESTS PASSED")


def test_receiver_getters():
    """Test receiver getter methods."""
    print("\n" + "=" * 60)
    print("Testing receiver getters")
    print("=" * 60)

    settings = SettingsModule()

    print(f"\n--- Testing receivers property ---")
    receivers = settings.receivers
    print(f"Number of receivers: {len(receivers)}")
    assert len(receivers) == 6, f"Expected 6 receivers, got {len(receivers)}"
    print("receivers property ... OK")

    print(f"\n--- Testing get_receiver() ---")
    for i in range(6):
        receiver = settings.get_receiver(i)
        assert receiver is not None, f"Receiver {i} not found"
        assert receiver['id'] == i, f"Receiver id mismatch"
        print(f"get_receiver({i}): id={receiver['id']}, position={receiver['position']}")
    print("get_receiver() ... OK")

    print(f"\n--- Testing get_receiver_position() ---")
    for i in range(6):
        pos = settings.get_receiver_position(i)
        assert pos is not None, f"Position for receiver {i} not found"
        assert len(pos) == 2, f"Position should have 2 elements"
        print(f"get_receiver_position({i}): {pos}")
    print("get_receiver_position() ... OK")

    print(f"\n--- Testing get_receiver_cal_distances() ---")
    for i in range(6):
        cal_dist = settings.get_receiver_cal_distances(i)
        assert cal_dist is not None, f"Cal distances for receiver {i} not found"
        print(f"get_receiver_cal_distances({i}): {cal_dist}")
    print("get_receiver_cal_distances() ... OK")

    print(f"\n--- Testing get_receiver_offset() ---")
    for i in range(6):
        offset = settings.get_receiver_offset(i)
        assert offset is not None, f"Offset for receiver {i} not found"
        assert 'slope' in offset, f"Offset missing slope"
        assert 'intercept' in offset, f"Offset missing intercept"
        print(f"get_receiver_offset({i}): slope={offset['slope']:.6f}, intercept={offset['intercept']:.6f}")
    print("get_receiver_offset() ... OK")

    print(f"\n--- Testing get_receiver_slope() ---")
    for i in range(6):
        slope = settings.get_receiver_slope(i)
        assert slope is not None, f"Slope for receiver {i} not found"
        print(f"get_receiver_slope({i}): {slope:.6f}")
    print("get_receiver_slope() ... OK")

    print(f"\n--- Testing get_receiver_intercept() ---")
    for i in range(6):
        intercept = settings.get_receiver_intercept(i)
        assert intercept is not None, f"Intercept for receiver {i} not found"
        print(f"get_receiver_intercept({i}): {intercept:.6f}")
    print("get_receiver_intercept() ... OK")

    print("\nReceiver getters: ALL TESTS PASSED")


def test_receiver_setters():
    """Test receiver setter methods."""
    print("\n" + "=" * 60)
    print("Testing receiver setters")
    print("=" * 60)

    settings = SettingsModule()

    # Store original values for receiver 0 to restore later
    original_position = settings.get_receiver_position(0)
    original_cal_distances = settings.get_receiver_cal_distances(0)
    original_offset = settings.get_receiver_offset(0)

    print(f"\n--- Testing set_receiver_position() ---")
    test_x, test_y = -100.5, -150.25
    settings.set_receiver_position(0, test_x, test_y)
    pos = settings.get_receiver_position(0)
    assert pos[0] == test_x and pos[1] == test_y, f"set_receiver_position failed"
    print(f"set_receiver_position(0, {test_x}, {test_y}) ... OK")

    # Verify persistence
    settings.reload_config()
    pos = settings.get_receiver_position(0)
    assert pos[0] == test_x and pos[1] == test_y, f"set_receiver_position not persisted"
    print(f"Position persisted after reload ... OK")

    print(f"\n--- Testing set_receiver_cal_distances() ---")
    test_distances = [111.1, 222.2]
    settings.set_receiver_cal_distances(0, test_distances)
    cal_dist = settings.get_receiver_cal_distances(0)
    assert cal_dist == test_distances, f"set_receiver_cal_distances failed"
    print(f"set_receiver_cal_distances(0, {test_distances}) ... OK")

    print(f"\n--- Testing set_receiver_offset() ---")
    test_slope, test_intercept = 0.123456, -45.6789
    settings.set_receiver_offset(0, test_slope, test_intercept)
    offset = settings.get_receiver_offset(0)
    assert offset['slope'] == test_slope, f"set_receiver_offset slope failed"
    assert offset['intercept'] == test_intercept, f"set_receiver_offset intercept failed"
    print(f"set_receiver_offset(0, {test_slope}, {test_intercept}) ... OK")

    print(f"\n--- Testing set_receiver_slope() ---")
    test_slope = 0.555555
    settings.set_receiver_slope(0, test_slope)
    slope = settings.get_receiver_slope(0)
    assert slope == test_slope, f"set_receiver_slope failed"
    print(f"set_receiver_slope(0, {test_slope}) ... OK")

    print(f"\n--- Testing set_receiver_intercept() ---")
    test_intercept = -99.9999
    settings.set_receiver_intercept(0, test_intercept)
    intercept = settings.get_receiver_intercept(0)
    assert intercept == test_intercept, f"set_receiver_intercept failed"
    print(f"set_receiver_intercept(0, {test_intercept}) ... OK")

    # Restore original values
    print(f"\n--- Restoring original values ---")
    settings.set_receiver_position(0, original_position[0], original_position[1])
    settings.set_receiver_cal_distances(0, original_cal_distances)
    settings.set_receiver_offset(0, original_offset['slope'], original_offset['intercept'])
    print("Original values restored ... OK")

    print("\nReceiver setters: ALL TESTS PASSED")


def test_legacy_methods():
    """Test legacy compatibility methods."""
    print("\n" + "=" * 60)
    print("Testing legacy methods")
    print("=" * 60)

    settings = SettingsModule()

    print(f"\n--- Testing get_tower_coordinates() ---")
    coords = settings.get_tower_coordinates()
    assert len(coords) == 6, f"Expected 6 coordinates, got {len(coords)}"
    for tower_id, (x, y) in coords:
        print(f"Tower {tower_id}: ({x}, {y})")
    print("get_tower_coordinates() ... OK")

    print(f"\n--- Testing arena_size ---")
    print(f"arena_size: {settings.arena_size}")
    assert len(settings.arena_size) == 2, "arena_size should have 2 elements"
    print("arena_size ... OK")

    print("\nLegacy methods: ALL TESTS PASSED")


def test_calculate_arena_size():
    """Test the _calculate_arena_size method."""
    print("\n" + "=" * 60)
    print("Testing _calculate_arena_size")
    print("=" * 60)

    settings = SettingsModule()

    # Get receiver positions for manual verification
    pos_1 = settings.get_receiver_position(0)  # Receiver 1
    pos_3 = settings.get_receiver_position(2)  # Receiver 3
    pos_4 = settings.get_receiver_position(3)  # Receiver 4
    pos_6 = settings.get_receiver_position(5)  # Receiver 6

    print(f"\n--- Receiver positions used for calculation ---")
    print(f"Receiver 1 (id 0): {pos_1}")
    print(f"Receiver 3 (id 2): {pos_3}")
    print(f"Receiver 4 (id 3): {pos_4}")
    print(f"Receiver 6 (id 5): {pos_6}")

    # Calculate expected values manually (rounded to hundredths)
    width_3_6 = abs(pos_3[0] - pos_6[0])
    width_1_4 = abs(pos_1[0] - pos_4[0])
    expected_width = round(max(width_3_6, width_1_4), 2)

    height_3_1 = abs(pos_3[1] - pos_1[1])
    height_6_4 = abs(pos_6[1] - pos_4[1])
    expected_height = round(max(height_3_1, height_6_4), 2)

    print(f"\n--- Width calculation (x-plane) ---")
    print(f"|Receiver 3 x - Receiver 6 x| = |{pos_3[0]} - {pos_6[0]}| = {width_3_6}")
    print(f"|Receiver 1 x - Receiver 4 x| = |{pos_1[0]} - {pos_4[0]}| = {width_1_4}")
    print(f"Width = max({width_3_6}, {width_1_4}) = {expected_width}")

    print(f"\n--- Height calculation (y-plane) ---")
    print(f"|Receiver 3 y - Receiver 1 y| = |{pos_3[1]} - {pos_1[1]}| = {height_3_1}")
    print(f"|Receiver 6 y - Receiver 4 y| = |{pos_6[1]} - {pos_4[1]}| = {height_6_4}")
    print(f"Height = max({height_3_1}, {height_6_4}) = {expected_height}")

    # Verify the calculated arena_size matches
    arena_size = settings.arena_size
    print(f"\n--- Verification ---")
    print(f"Calculated arena_size: {arena_size}")
    print(f"Expected: ({expected_width}, {expected_height})")

    assert arena_size[0] == expected_width, f"Width mismatch: {arena_size[0]} != {expected_width}"
    assert arena_size[1] == expected_height, f"Height mismatch: {arena_size[1]} != {expected_height}"
    print("arena_size matches expected values ... OK")

    # Verify arena_size is positive
    assert arena_size[0] > 0, "Width should be positive"
    assert arena_size[1] > 0, "Height should be positive"
    print("arena_size values are positive ... OK")

    print("\n_calculate_arena_size: ALL TESTS PASSED")


def test_error_handling():
    """Test error handling for invalid operations."""
    print("\n" + "=" * 60)
    print("Testing error handling")
    print("=" * 60)

    settings = SettingsModule()

    print(f"\n--- Testing invalid receiver ID ---")
    try:
        settings.set_receiver_position(999, 0, 0)
        print("ERROR: Should have raised ValueError")
    except ValueError as e:
        print(f"set_receiver_position(999, 0, 0) raised ValueError: {e} ... OK")

    try:
        settings.set_receiver_cal_distances(999, [1, 2])
        print("ERROR: Should have raised ValueError")
    except ValueError as e:
        print(f"set_receiver_cal_distances(999, ...) raised ValueError ... OK")

    try:
        settings.set_receiver_offset(999, 0, 0)
        print("ERROR: Should have raised ValueError")
    except ValueError as e:
        print(f"set_receiver_offset(999, ...) raised ValueError ... OK")

    try:
        settings.set_receiver_slope(999, 0)
        print("ERROR: Should have raised ValueError")
    except ValueError as e:
        print(f"set_receiver_slope(999, ...) raised ValueError ... OK")

    try:
        settings.set_receiver_intercept(999, 0)
        print("ERROR: Should have raised ValueError")
    except ValueError as e:
        print(f"set_receiver_intercept(999, ...) raised ValueError ... OK")

    print(f"\n--- Testing get with invalid receiver ID ---")
    result = settings.get_receiver(999)
    assert result is None, "get_receiver(999) should return None"
    print("get_receiver(999) returns None ... OK")

    result = settings.get_receiver_position(999)
    assert result is None, "get_receiver_position(999) should return None"
    print("get_receiver_position(999) returns None ... OK")

    print("\nError handling: ALL TESTS PASSED")


def test_reload_config():
    """Test the reload_config method."""
    print("\n" + "=" * 60)
    print("Testing reload_config")
    print("=" * 60)

    settings = SettingsModule()

    # Store original value
    original_cal_state = settings.cal_state

    # Modify internal config without saving
    settings._config['cal_state'] = 12345

    # Verify internal change
    assert settings.cal_state == 12345, "Internal modification failed"
    print(f"Internal modification: cal_state = 12345 ... OK")

    # Reload from file (should restore original value)
    settings.reload_config()
    assert settings.cal_state == original_cal_state, "reload_config failed to restore value"
    print(f"After reload_config(): cal_state = {settings.cal_state} (restored) ... OK")

    print("\nreload_config: ALL TESTS PASSED")


def plot_serial_values(ax, lines, data):
    """
    Update the live serial value plot with the current rolling data.

    Args:
        ax: Matplotlib Axes object
        lines: List of Line2D objects, one per receiver
        data: List of deques containing recent serial values per receiver
    """
    for line, values in zip(lines, data):
        x = list(range(len(values)))
        line.set_xdata(x)
        line.set_ydata(list(values))
    ax.relim()
    ax.autoscale_view()
    ax.figure.canvas.flush_events()
    plt.pause(0.001)


def run_serial_monitor(ip_address="127.0.0.1"):
    """
    Connect to the UltraGPS server and display incoming serial values live.

    Each receiver's serial reading is shown as a separate line on a rolling
    time-series plot. Press Ctrl+C to stop.

    Args:
        ip_address (str): IP address of the UltraGPS server
    """
    control = ControlModule(ip_address=ip_address)
    receiver_count = control.receiver_count
    data = [deque(maxlen=PLOT_HISTORY) for _ in range(receiver_count)]

    plt.ion()
    fig, ax = plt.subplots(figsize=(10, 5))
    lines = []
    for i in range(receiver_count):
        line, = ax.plot([], [], label=f"Receiver {i + 1}")
        lines.append(line)

    ax.set_xlabel("Sample")
    ax.set_ylabel("Serial Value")
    ax.set_title("Live Serial Values from UltraGPS")
    ax.legend()
    plt.tight_layout()
    plt.show()

    print(f"Connected to UltraGPS server at {ip_address}. Reading serial values (Ctrl+C to stop)...")
    try:
        while True:
            control.update()
            serial_msg = control.get_serial_message()

            try:
                values = serial_msg.strip().split()
                if len(values) >= receiver_count:
                    floats = [float(values[i]) for i in range(receiver_count)]
                    print(serial_msg.strip())
                    for i, v in enumerate(floats):
                        data[i].append(v)
                    plot_serial_values(ax, lines, data)
            except (ValueError, AttributeError) as e:
                print(f"Error parsing serial message '{serial_msg}': {e}")

    except KeyboardInterrupt:
        print("\nMonitor stopped.")
    finally:
        control.comms_module.close()
        plt.ioff()
        plt.show()


def main():
    """Run all tests, then start the live serial monitor."""
    parser = argparse.ArgumentParser(description="SettingsModule tests + live serial monitor")
    parser.add_argument("--ip", default="127.0.0.1", help="IP address of the UltraGPS server (default: 127.0.0.1)")
    parser.add_argument("--skip-tests", action="store_true", help="Skip settings tests and jump straight to serial monitor")
    args = parser.parse_args()

    if not args.skip_tests:
        print("\n" + "#" * 60)
        print("# SettingsModule Test Suite")
        print("#" * 60)

        try:
            test_top_level_properties()
            test_receiver_getters()
            test_receiver_setters()
            test_legacy_methods()
            test_calculate_arena_size()
            test_error_handling()
            test_reload_config()

            print("\n" + "#" * 60)
            print("# ALL TESTS PASSED!")
            print("#" * 60 + "\n")

        except AssertionError as e:
            print(f"\n!!! TEST FAILED: {e}")
            raise
        except Exception as e:
            print(f"\n!!! UNEXPECTED ERROR: {e}")
            raise

    run_serial_monitor(ip_address=args.ip)


if __name__ == "__main__":
    main()
