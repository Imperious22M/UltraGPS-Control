import time
import statistics
import argparse
from ControlModule import ControlModule

def test_receiver_distance_speed(num_iterations=100, ip_address="127.0.0.1"):
    """
    Test how fast new receiver distances can be received.

    Args:
        num_iterations (int): Number of update cycles to perform
        ip_address (str): IP address of the UltraGPS server
    """
    print("Initializing ControlModule...")
    control_module = ControlModule(ip_address=ip_address, receiver_count=6)
    
    # Warm-up: do a few updates to establish connection
    print("Warming up connection...")
    for _ in range(3):
        try:
            control_module.update()
            time.sleep(0.1)
        except Exception as e:
            print(f"Warning during warm-up: {e}")
    
    print(f"\nTesting receiver distance update speed ({num_iterations} iterations)...")
    print("-" * 60)
    
    update_times = []
    distances_list = []
    
    start_time = time.time()
    
    for i in range(num_iterations):
        iteration_start = time.time()
        
        try:
            # Update to get new distances
            control_module.update()
            
            # Get the receiver distances
            distances = control_module.get_receiver_distances()
            
            iteration_time = time.time() - iteration_start
            update_times.append(iteration_time)
            distances_list.append(distances)
            
            # Print progress every 10 iterations
            if (i + 1) % 10 == 0:
                avg_time = statistics.mean(update_times[-10:])
                print(f"Iteration {i + 1}/{num_iterations}: Last update took {iteration_time*1000:.2f} ms, "
                      f"Avg of last 10: {avg_time*1000:.2f} ms")
        
        except Exception as e:
            print(f"Error at iteration {i + 1}: {e}")
            continue
    
    total_time = time.time() - start_time
    
    # Calculate statistics
    if update_times:
        avg_update_time = statistics.mean(update_times)
        median_update_time = statistics.median(update_times)
        min_update_time = min(update_times)
        max_update_time = max(update_times)
        std_dev = statistics.stdev(update_times) if len(update_times) > 1 else 0
        
        updates_per_second = 1.0 / avg_update_time if avg_update_time > 0 else 0
        actual_rate = num_iterations / total_time if total_time > 0 else 0
        
        print("\n" + "=" * 60)
        print("PERFORMANCE STATISTICS")
        print("=" * 60)
        print(f"Total iterations: {num_iterations}")
        print(f"Total time: {total_time:.3f} seconds")
        print(f"\nUpdate Time Statistics:")
        print(f"  Average: {avg_update_time*1000:.2f} ms")
        print(f"  Median:  {median_update_time*1000:.2f} ms")
        print(f"  Minimum: {min_update_time*1000:.2f} ms")
        print(f"  Maximum: {max_update_time*1000:.2f} ms")
        print(f"  Std Dev: {std_dev*1000:.2f} ms")
        print(f"\nUpdate Rate:")
        print(f"  Theoretical max: {updates_per_second:.2f} updates/second")
        print(f"  Actual rate:     {actual_rate:.2f} updates/second")
        
        # Show sample distances from first and last iteration
        if distances_list:
            print(f"\nSample distances (first iteration): {distances_list[0]}")
            if len(distances_list) > 1:
                print(f"Sample distances (last iteration):  {distances_list[-1]}")
    else:
        print("\nNo successful updates recorded!")
    
    # Cleanup
    try:
        control_module.comms_module.close()
    except:
        pass
    
    print("\nTest complete!")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="UltraGPS control module speed test")
    parser.add_argument("--ip", default="127.0.0.1", help="IP address of the UltraGPS server (default: 127.0.0.1)")
    parser.add_argument("--iterations", type=int, default=100, help="Number of update cycles to perform (default: 100)")
    args = parser.parse_args()

    test_receiver_distance_speed(num_iterations=args.iterations, ip_address=args.ip)

