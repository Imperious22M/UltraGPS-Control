import socket
import threading
import queue
from collections import defaultdict


class ControlModule:
    def __init__(self, ip_address="127.0.0.1", receiver_count = 6):
        """
        Initializes the ControlModule with the networking needed to control the positioning system

        Args:
            default_ip_address (str, optional): Default IP address of the UltraGPS server
        """
        self.ip_address = ip_address
        self.receiver_count = receiver_count

        self.TIMEOUT  = None
        self.SEPARATOR = " "
        self.CONTROL_PORT = 8000
        self.DISTANCE_PORT = 8002
        self.RAWSERIAL_PORT = 8003


        self.comms_module:CommsModule = CommsModule(self.ip_address)

        # Start receiving on UltraGPS server ports
        self.comms_module.start_receiving(self.DISTANCE_PORT)
        self.comms_module.start_receiving(self.RAWSERIAL_PORT)
        #self.controlModule.queue_udp_message("P\n",8000,"127.0.0.1")
        #print(controlModule.receive_udp_message(8002))

        # Information provided by the UltraGPS server every requeest
        self.distances = None
        self.serial_message = None

    def update(self):
        """
        Updates all the module information by requesting it from the server 

        """

        self.comms_module.queue_udp_message("P\n",self.CONTROL_PORT)
        distance_bytes = self.comms_module.receive_udp_message(self.DISTANCE_PORT)
        distances_str = distance_bytes[0].decode('utf-8').rstrip("\n").rstrip(" ").split(self.SEPARATOR)
        self.distances = tuple(map(float, distances_str ))
        self.serial_message = self.comms_module.receive_udp_message(self.RAWSERIAL_PORT)[0].decode('utf-8')

    def get_receiver_distances(self):
        """
        Returns a list of all the distances received by the receivers
        DOES NOT update the information of the system

        """

        if len(self.distances) != self.receiver_count:
            print("Distances received from UltraGPS does not match number of receivers")
            self.distances = []
            return tuple()
        return self.distances

    def get_serial_message(self):
        return self.serial_message

class CommsModule:
    def __init__(self, default_ip_address=None):
        """
        Initialize the CommsModule with threading and buffers for UDP communication.
        
        Args:
            default_ip_address (str, optional): Default IP address for sending messages
        """
        self.default_ip_address = default_ip_address
        self.send_queue = queue.Queue()
        self.receive_buffers = defaultdict(queue.Queue)  # port -> queue of (data, address) tuples
        self.network_instances = {}  # (ip, port) -> NetworkClass instance
        self.receive_threads = {}  # port -> thread
        self.lock = threading.Lock()
        
        # Start the sending thread
        self.send_thread = threading.Thread(target=self._send_worker, daemon=True)
        self.running = True
        self.send_thread.start()
    
    def _get_network_instance(self, ip_address, port):
        """
        Get or create a NetworkClass instance for the given IP and port.
        
        Args:
            ip_address (str): IP address
            port (int): Port number
            
        Returns:
            NetworkClass: Network instance for this IP/port combination
        """
        key = (ip_address, port)
        with self.lock:
            if key not in self.network_instances:
                network = NetworkClass(ip_address=ip_address, port=port)
                self.network_instances[key] = network
        return self.network_instances[key]
    
    def _send_worker(self):
        """Worker thread that processes the send queue."""
        while self.running:
            try:
                # Get message from queue with timeout to allow checking self.running
                item = self.send_queue.get(timeout=0.1)
                message, ip_address, port = item
                
                try:
                    network = self._get_network_instance(ip_address, port)
                    network.send_udp_message(message, ip_address=ip_address, port=port)
                except Exception as e:
                    # Log error but continue processing
                    print(f"Error sending UDP message: {e}")
                finally:
                    self.send_queue.task_done()
            except queue.Empty:
                continue
    
    def _receive_worker(self, port, ip_address=None):
        """
        Worker thread that receives UDP messages for a specific port.
        
        Args:
            port (int): Port to receive on
            ip_address (str, optional): IP address to bind to (None for any)
        """
        network = self._get_network_instance(ip_address or '0.0.0.0', port)
        
        # Bind to the port for receiving
        try:
            network.bind_to_port(port)
        except Exception as e:
            print(f"Error binding to port {port}: {e}")
            return
        
        while self.running and port in self.receive_threads:
            try:
                data, address = network.receive_udp_message(timeout=0.1)
                self.receive_buffers[port].put((data, address))
            except socket.timeout:
                continue
            except Exception as e:
                # Log error but continue receiving
                print(f"Error receiving UDP message on port {port}: {e}")
                continue
    
    def queue_udp_message(self, message, port, ip_address=None):
        """
        Queue a UDP message for transmission.
        
        Args:
            message (str or bytes): The message to send
            port (int): The port number to send to
            ip_address (str, optional): The IP address to send to (uses default if not specified)
        """
        target_ip = ip_address or self.default_ip_address
        if target_ip is None:
            raise ValueError("IP address must be specified either in __init__ or as an argument to queue_udp_message")
        
        self.send_queue.put((message, target_ip, port))
    
    def receive_udp_message(self, port, timeout=None):
        """
        Receive a UDP message from the buffer for the specified port.
        Automatically starts receiving on the port if not already started.
        
        Args:
            port (int): The port number to receive from
            timeout (float, optional): Timeout in seconds (None for blocking)
            
        Returns:
            tuple: (data, address) where data is bytes and address is (ip, port)
                   Returns None if timeout occurs
        """
        # Automatically start receiving if not already started
        if port not in self.receive_threads:
            self.start_receiving(port)
        
        try:
            if timeout is None:
                data, address = self.receive_buffers[port].get()
            else:
                data, address = self.receive_buffers[port].get(timeout=timeout)
            return data, address
        except queue.Empty:
            return None
    
    def start_receiving(self, port, ip_address=None):
        """
        Start a receive thread for the specified port.
        
        Args:
            port (int): Port number to receive on
            ip_address (str, optional): IP address to bind to (None for any)
        """
        if port in self.receive_threads:
            return  # Already receiving on this port
        
        with self.lock:
            if port not in self.receive_threads:
                thread = threading.Thread(
                    target=self._receive_worker,
                    args=(port, ip_address),
                    daemon=True
                )
                thread.start()
                self.receive_threads[port] = thread
    
    def stop_receiving(self, port):
        """
        Stop receiving on the specified port.
        
        Args:
            port (int): Port number to stop receiving on
        """
        with self.lock:
            if port in self.receive_threads:
                del self.receive_threads[port]
    
    def close(self):
        """Stop all threads and close all network connections."""
        self.running = False
        
        # Wait for send thread to finish
        if self.send_thread.is_alive():
            self.send_thread.join(timeout=1.0)
        
        # Stop all receive threads
        ports_to_stop = list(self.receive_threads.keys())
        for port in ports_to_stop:
            self.stop_receiving(port)
        
        # Wait for receive threads to finish
        for thread in list(self.receive_threads.values()):
            if thread.is_alive():
                thread.join(timeout=1.0)
        
        # Close all network instances
        for network in self.network_instances.values():
            network.close()
        
        self.network_instances.clear()
        self.receive_threads.clear()
    
    def __del__(self):
        """Clean up when object is destroyed."""
        self.close()

class NetworkClass:
    def __init__(self, ip_address=None, port=None):
        """
        Initialize the ControlModule with optional IP address and port.
        
        Args:
            ip_address (str, optional): The IP address to send UDP messages to
            port (int, optional): The port number for UDP communication
        """
        self.ip_address = ip_address
        self.port = port
        self.socket = None
        
    def _get_socket(self):
        """Create and return a UDP socket if one doesn't exist."""
        if self.socket is None:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            # Enable SO_REUSEADDR to allow binding to shared ports
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            # Enable SO_REUSEPORT if available (Linux)
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (AttributeError, OSError):
                # SO_REUSEPORT not available on this platform, skip it
                pass
        return self.socket
    
    def set_ip_address(self, ip_address):
        """
        Set or update the IP address for sending UDP messages.
        
        Args:
            ip_address (str): The IP address to send UDP messages to
        """
        self.ip_address = ip_address
    
    def set_port(self, port):
        """
        Set or update the port number for UDP communication.
        
        Args:
            port (int): The port number for UDP communication
        """
        self.port = port
    
    def send_udp_message(self, message, ip_address=None, port=None):
        """
        Send a UDP message to the specified IP address and port.
        
        Args:
            message (str or bytes): The message to send
            ip_address (str, optional): Override the IP address for this message
            port (int, optional): Override the port for this message
            
        Returns:
            int: Number of bytes sent
            
        Raises:
            ValueError: If IP address or port is not specified
        """
        target_ip = ip_address or self.ip_address
        target_port = port or self.port
        
        if target_ip is None:
            raise ValueError("IP address must be specified either in __init__, set_ip_address(), or as an argument")
        if target_port is None:
            raise ValueError("Port must be specified either in __init__, set_port(), or as an argument")
        
        sock = self._get_socket()
        
        # Convert string message to bytes if necessary
        if isinstance(message, str):
            message = message.encode('utf-8')
        
        bytes_sent = sock.sendto(message, (target_ip, target_port))
        return bytes_sent
    
    def receive_udp_message(self, buffer_size=1024, timeout=None):
        """
        Receive a UDP message from any sender.
        
        Args:
            buffer_size (int): Maximum number of bytes to receive (default: 1024)
            timeout (float, optional): Timeout in seconds for the receive operation
            
        Returns:
            tuple: (data, address) where data is bytes and address is (ip, port)
            
        Raises:
            socket.timeout: If timeout is set and no message is received
        """
        sock = self._get_socket()
        
        if timeout is not None:
            sock.settimeout(timeout)
        
        try:
            data, address = sock.recvfrom(buffer_size)
            return data, address
        finally:
            if timeout is not None:
                sock.settimeout(None)
    
    def bind_to_port(self, port=None):
        """
        Bind the socket to a specific port for receiving messages.
        
        Args:
            port (int, optional): Port to bind to (uses self.port if not specified)
            
        Raises:
            ValueError: If port is not specified
        """
        target_port = port or self.port
        if target_port is None:
            raise ValueError("Port must be specified either in __init__, set_port(), or as an argument")
        
        sock = self._get_socket()
        sock.bind(('', target_port))
    
    def close(self):
        """Close the UDP socket."""
        if self.socket is not None:
            self.socket.close()
            self.socket = None
    
    def __del__(self):
        """Clean up socket when object is destroyed."""
        self.close()

class CalibrateSystem:
    """
    Standalone calibration system that can run calibration without graphical elements.
    Collects serial messages and calculates offset values for each receiver.
    """

    def __init__(self, control_module: ControlModule):
        """
        Initialize the CalibrateSystem with a control module for communication.

        Args:
            control_module: ControlModule instance for UDP communication
        """
        self.control_module = control_module
        self.receiver_count = control_module.receiver_count

    def run_calibration(self, min_reads):
        """
        Perform a single calibration run, collecting serial messages until
        min_reads threshold is met for all receivers.

        This is a standalone function that does not use any graphical elements.

        Args:
            min_reads: Minimum number of identical readings required for each receiver

        Returns:
            dict: {receiver_id: most_frequent_serial_value} for each receiver
        """
        # Data storage: {receiver_id: {serial_value: count}}
        data = {i: {} for i in range(self.receiver_count)}

        # Initial read to clear network
        try:
            self.control_module.update()
            _ = self.control_module.get_serial_message()
        except Exception as e:
            print(f"Error clearing network: {e}")

        # Keep reading until all receivers have met min_reads threshold
        while True:
            try:
                # Request new reading
                self.control_module.update()
                serial_msg = self.control_module.get_serial_message()

                # Parse serial message - assume space-separated values, one per receiver
                try:
                    serial_values = serial_msg.strip().split()
                    if len(serial_values) >= self.receiver_count:
                        for recv_id in range(self.receiver_count):
                            try:
                                value = float(serial_values[recv_id])
                                if value in data[recv_id]:
                                    data[recv_id][value] += 1
                                else:
                                    data[recv_id][value] = 1
                            except (ValueError, IndexError):
                                pass
                except Exception as e:
                    print(f"Error parsing serial message: {e}")
                    continue

                # Check if all receivers have met min_reads threshold
                all_met = True
                for recv_id in range(self.receiver_count):
                    if not data[recv_id]:
                        all_met = False
                        break
                    max_count = max(data[recv_id].values())
                    if max_count < min_reads:
                        all_met = False
                        break

                if all_met:
                    break

            except Exception as e:
                print(f"Error in calibration run: {e}")
                break

        # Return most frequent value for each receiver
        result = {}
        for recv_id in range(self.receiver_count):
            if data[recv_id]:
                most_frequent = max(data[recv_id].items(), key=lambda x: x[1])[0]
                result[recv_id] = most_frequent
            else:
                result[recv_id] = None

        return result

    def calculate_offsets(self, run1_values, run2_values, known_distances):
        """
        Calculate slope (a) and intercept (b) offsets for each receiver.

        Formula:
            a = (known_dist_1 - known_dist_2) / (valid_count_1 - valid_count_2)
            b = known_dist_1 - (a * valid_count_1)

        Args:
            run1_values: dict {receiver_id: serial_value} from run 1
            run2_values: dict {receiver_id: serial_value} from run 2
            known_distances: dict {receiver_id: (dist_to_cal_point_1, dist_to_cal_point_2)}

        Returns:
            dict: {receiver_id: {'slope': a, 'intercept': b}} for each receiver
        """
        offsets = {}

        for recv_id in range(self.receiver_count):
            if recv_id not in run1_values or recv_id not in run2_values:
                print(f"Missing calibration data for receiver {recv_id}")
                continue

            if recv_id not in known_distances:
                print(f"Missing known distances for receiver {recv_id}")
                continue

            valid_count_1 = run1_values[recv_id]
            valid_count_2 = run2_values[recv_id]

            if valid_count_1 is None or valid_count_2 is None:
                print(f"Missing serial values for receiver {recv_id}")
                continue

            known_dist_1, known_dist_2 = known_distances[recv_id]

            try:
                if valid_count_1 == valid_count_2:
                    print(f"Warning: Same serial value for both runs on receiver {recv_id}")
                    continue

                a = (known_dist_1 - known_dist_2) / (valid_count_1 - valid_count_2)
                b = known_dist_1 - (a * valid_count_1)

                offsets[recv_id] = {'slope': a, 'intercept': b}

            except Exception as e:
                print(f"Error calculating offset for receiver {recv_id}: {e}")

        return offsets

    def full_calibration(self, min_reads, known_distances):
        """
        Perform a complete two-run calibration and calculate offsets.

        Note: This is a blocking operation that requires user interaction
        to position the transmitter between runs. For automated testing,
        use run_calibration() and calculate_offsets() separately.

        Args:
            min_reads: Minimum number of identical readings required for each receiver
            known_distances: dict {receiver_id: (dist_to_cal_point_1, dist_to_cal_point_2)}

        Returns:
            dict: {receiver_id: {'slope': a, 'intercept': b}} for each receiver
        """
        print("Starting calibration run 1...")
        print("Please place transmitter at calibration point 1")
        input("Press Enter when ready...")

        run1_values = self.run_calibration(min_reads)
        print(f"Run 1 complete: {run1_values}")

        print("\nStarting calibration run 2...")
        print("Please place transmitter at calibration point 2")
        input("Press Enter when ready...")

        run2_values = self.run_calibration(min_reads)
        print(f"Run 2 complete: {run2_values}")

        offsets = self.calculate_offsets(run1_values, run2_values, known_distances)
        print(f"\nCalculated offsets: {offsets}")

        return offsets