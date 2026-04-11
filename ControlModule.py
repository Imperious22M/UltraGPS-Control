import socket
import threading
import queue
from collections import defaultdict


class ControlModule:
    def __init__(self, ip_address="127.0.0.1"):
        """
        Initializes the ControlModule with the networking needed to control the positioning system

        Args:
            ip_address (str, optional): IP address of the UltraGPS-Ground server
        """
        self.ip_address = ip_address

        self.SEPARATOR = " "
        self.TCP_PORT = 9000   # TCP: send commands (P/S/C), receive responses
        self.UDP_PORT = 9001   # UDP: receive continuous streaming data

        self.comms_module: CommsModule = CommsModule(self.ip_address)

        self.serial_message = None

    def update(self):
        """
        Sends a normal poll command to the server via TCP and stores the response.
        """
        command = "P\n"
        self.comms_module.send_from_port(command, self.TCP_PORT)
        response_bytes = self.comms_module.receive_tcp_message(self.TCP_PORT)
        if response_bytes:
            self.serial_message = response_bytes.decode('utf-8')
            # Strip the leading message response (e.g. "N: :) 
            self.serial_message = self.serial_message[3:].strip()
        else:
            self.serial_message = None


    def get_serial_message(self):
        return self.serial_message

class CommsModule:
    def __init__(self, default_ip_address=None):
        """
        Initialize the CommsModule with threading and buffers for TCP and UDP communication.
        Acts as a pure client: TCP connects to the server's command port, UDP sends to the
        server's streaming port and receives responses on the same ephemeral socket.

        Args:
            default_ip_address (str, optional): Default IP address of the UltraGPS-Ground server
        """
        self.default_ip_address = default_ip_address
        self.running = True
        self.lock = threading.Lock()

        # TCP
        self.tcp_receive_buffers = defaultdict(queue.Queue)  # TCP port -> queue of bytes
        self.tcp_instances = {}       # (ip, port) -> TCPNetworkClass
        self.tcp_receive_threads = {}  # TCP port -> thread

        # UDP client — single socket with an OS-assigned ephemeral local port
        self.udp_socket = None
        self.udp_socket_lock = threading.Lock()
        self.udp_receive_buffer = queue.Queue()
        self.udp_receive_thread = None

    def _get_tcp_instance(self, ip_address, port):
        """
        Get or create a connected TCPNetworkClass instance for the given server IP/port.
        Also starts a receive thread for incoming data on that connection.

        Args:
            ip_address (str): Server IP address
            port (int): Server TCP port

        Returns:
            TCPNetworkClass: Connected TCP instance
        """
        key = (ip_address, port)
        with self.lock:
            if key not in self.tcp_instances:
                tcp = TCPNetworkClass(ip_address, port)
                tcp.connect()
                self.tcp_instances[key] = tcp
                thread = threading.Thread(
                    target=self._tcp_receive_worker,
                    args=(tcp, port),
                    daemon=True
                )
                thread.start()
                self.tcp_receive_threads[port] = thread
        return self.tcp_instances[key]

    def _tcp_receive_worker(self, tcp_net, port):
        """Worker thread that continuously reads from a TCP connection into a buffer."""
        while self.running:
            try:
                data = tcp_net.receive_tcp_message(timeout=0.1)
                if data:
                    self.tcp_receive_buffers[port].put(data)
            except socket.timeout:
                continue
            except Exception as e:
                print(f"Error receiving TCP message on port {port}: {e}")
                continue

    def send_from_port(self, message, tcp_port, ip_address=None):
        """
        Send a command to the server via TCP.
        The server will respond on the same TCP connection; call receive_tcp_message()
        to dequeue the response.

        Args:
            message (str or bytes): The command to send (e.g. "P\\n", "S\\n", "C\\n")
            tcp_port (int): The server's TCP command port (default 9000)
            ip_address (str, optional): Server IP address (uses default if not specified)
        """
        target_ip = ip_address or self.default_ip_address
        if target_ip is None:
            raise ValueError("IP address must be specified either in __init__ or as an argument")

        tcp = self._get_tcp_instance(target_ip, tcp_port)
        tcp.send_tcp_message(message)

    def receive_tcp_message(self, tcp_port, timeout=None):
        """
        Dequeue the next TCP response received from the server on the given port.

        Args:
            tcp_port (int): The TCP port the connection is on
            timeout (float, optional): Timeout in seconds (None for blocking)

        Returns:
            bytes: Response data, or None if timeout occurs
        """
        try:
            if timeout is None:
                return self.tcp_receive_buffers[tcp_port].get()
            else:
                return self.tcp_receive_buffers[tcp_port].get(timeout=timeout)
        except queue.Empty:
            return None
    
    def _get_udp_socket(self):
        """
        Get or create the UDP client socket, starting the receive thread on first use.
        The socket is not bound to any fixed local port; the OS assigns an ephemeral port
        when the first sendto() call is made, and the server replies to that port.
        """
        with self.udp_socket_lock:
            if self.udp_socket is None:
                self.udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.udp_receive_thread = threading.Thread(
                    target=self._udp_receive_worker,
                    daemon=True
                )
                self.udp_receive_thread.start()
        return self.udp_socket

    def send_udp(self, message, server_port, ip_address=None):
        """
        Send a UDP datagram to the server's streaming port.
        The first call establishes our ephemeral local port; the server records it and
        sends continuous data back to that port.

        Args:
            message (str or bytes): The datagram payload
            server_port (int): The server's UDP port (default 9001)
            ip_address (str, optional): Server IP address (uses default if not specified)
        """
        target_ip = ip_address or self.default_ip_address
        if target_ip is None:
            raise ValueError("IP address must be specified either in __init__ or as an argument")
        if isinstance(message, str):
            message = message.encode('utf-8')
        self._get_udp_socket().sendto(message, (target_ip, server_port))

    def _udp_receive_worker(self):
        """Receive UDP datagrams from the server on the client socket and buffer them."""
        while self.running:
            try:
                self.udp_socket.settimeout(0.1)
                data, addr = self.udp_socket.recvfrom(4096)
                if data:
                    self.udp_receive_buffer.put((data, addr))
            except socket.timeout:
                continue
            except Exception as e:
                print(f"Error receiving UDP message: {e}")
                continue

    def receive_udp_message(self, timeout=None):
        """
        Dequeue the next UDP datagram received from the server.

        Args:
            timeout (float, optional): Timeout in seconds (None for blocking)

        Returns:
            tuple: (data, address) where data is bytes and address is (ip, port),
                   or None if timeout occurs
        """
        try:
            if timeout is None:
                return self.udp_receive_buffer.get()
            else:
                return self.udp_receive_buffer.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self):
        """Stop all threads and close all connections."""
        self.running = False

        # Wait for TCP receive threads
        for thread in self.tcp_receive_threads.values():
            if thread.is_alive():
                thread.join(timeout=1.0)

        # Close all TCP connections
        for tcp in self.tcp_instances.values():
            tcp.close()
        self.tcp_instances.clear()
        self.tcp_receive_threads.clear()

        # Wait for UDP receive thread and close socket
        if self.udp_receive_thread and self.udp_receive_thread.is_alive():
            self.udp_receive_thread.join(timeout=1.0)
        with self.udp_socket_lock:
            if self.udp_socket is not None:
                self.udp_socket.close()
                self.udp_socket = None
    
    def __del__(self):
        """Clean up when object is destroyed."""
        self.close()

class TCPNetworkClass:
    def __init__(self, ip_address, port):
        """
        Manages a persistent TCP connection to the UltraGPS-Ground server.

        Args:
            ip_address (str): Server IP address
            port (int): Server TCP port
        """
        self.ip_address = ip_address
        self.port = port
        self.socket = None
        self.send_lock = threading.Lock()

    def connect(self):
        """Open a TCP connection to the server."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((self.ip_address, self.port))
        self.socket = sock

    def send_tcp_message(self, message):
        """
        Send a message over the TCP connection.

        Args:
            message (str or bytes): The message to send
        """
        if isinstance(message, str):
            message = message.encode('utf-8')
        with self.send_lock:
            self.socket.sendall(message)

    def receive_tcp_message(self, buffer_size=4096, timeout=None):
        """
        Receive a message from the TCP connection.

        Args:
            buffer_size (int): Maximum bytes to read per call
            timeout (float, optional): Socket timeout in seconds

        Returns:
            bytes: Received data, or None if the connection was closed

        Raises:
            socket.timeout: If timeout is set and no data arrives in time
        """
        if timeout is not None:
            self.socket.settimeout(timeout)
        try:
            data = self.socket.recv(buffer_size)
            return data if data else None
        finally:
            if timeout is not None:
                self.socket.settimeout(None)

    def close(self):
        """Close the TCP connection."""
        if self.socket is not None:
            self.socket.close()
            self.socket = None

    def __del__(self):
        self.close()

class CalibrateSystem:
    """
    Standalone calibration system that can run calibration without graphical elements.
    Collects serial messages and calculates offset values for each receiver.
    """

    def __init__(self, control_module: ControlModule, receiver_count: int):
        """
        Initialize the CalibrateSystem with a control module for communication.

        Args:
            control_module: ControlModule instance for communication
            receiver_count: Number of receivers in the system
        """
        self.control_module = control_module
        self.receiver_count = receiver_count

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