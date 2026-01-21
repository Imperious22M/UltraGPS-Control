from random import randint
import matplotlib
matplotlib.use('TkAgg')  # Use TkAgg backend for tkinter integration
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from collections import deque
import tkinter as tk
import threading
import queue
import time
from ControlModule import ControlModule
from PositionModule import PositionModule
from SettingsModule import SettingsModule

class GraphicsModule:
    def __init__(self):
        """
        Initialize the GraphicsModule with a tkinter root window running in a background thread.
        """
        self.root = None
        self.position_window = None
        self.update_queue = queue.Queue()
        self.running = True
        self.active_threads = [] # Active threads for currently active windows
        self.active_animations = [] #FIFO list of active animations that need refreshing

        # Instnatiate control module
        self.control_module = ControlModule("127.0.0.1")

        # Instantiate the settings module
        self.settings_module = SettingsModule()

        # Instantiate matplotlib window classes
        self.position_window = PositionWindow(
                                grid_width=self.settings_module.arena_size[0],
                                grid_height=self.settings_module.arena_size[1],
                                receiver_positions=self.settings_module.get_tower_coordinates()
                                )

    def start_tk_window(self):
        """ 
        Setup the tk window and necessary hooks
        """
        self.root = tk.Tk()
        self.root.title("UltraGPS Control")
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)

    def tkinter_main(self):
        """
        Main function for the tkinter thread.
        All tkinter operations must happen in this thread.
        """
        
        # Schedule periodic queue processing
        self._process_queue()
        # Schedule periodic window refreshing
        self._refresh_animations()
        
        # Start the mainloop
        self.root.mainloop()
        # Set running false to prevent scheduling extra tasks
        self.running = False
    
    def _process_queue(self):
        """Process update queue in the tkinter thread, every 10ms."""
        # Check if we should continue processing
        if not self.running or not self.root:
            return
        
        try:
            while True:
                try:
                    callback = self.update_queue.get_nowait()
                    callback()
                except queue.Empty:
                    break
        except Exception as e:
            print(f"Error processing queue: {e}")
        
        # Schedule next check only if still running and root exists
        if self.running and self.root:
            try:
                self.root.after(10, self._process_queue)
            except tk.TclError:
                # Root window was destroyed, stop processing
                self.running = False
    
    def _on_closing(self):
        """Handle window closing event."""
        self.running = False
        if self.root:
            self.root.quit()
            # Don't destroy immediately - let mainloop finish
            # The destroy will happen after mainloop exits
    
    def _refresh_animations(self):
        """
        Refresh all animations at a set rate.
        All canvases (including position window) are in active_animations list.
        """
        # Check if we should continue processing
        if not self.running or not self.root:
            return

        # Refresh all active animation canvases
        for canvas in self.active_animations:
            if canvas:
                try:
                    canvas.draw()
                except Exception as e:
                    print(f"Error refreshing animation: {e}")

        # Schedule next refresh only if still running and root exists
        if self.running and self.root:
            try:
                self.root.after(10, self._refresh_animations)
            except tk.TclError:
                # Root window was destroyed, stop processing
                self.running = False

    def _schedule_update(self, callback):
        """
        Schedule a callback to run in the tkinter thread.
        
        Args:
            callback (callable): Function to execute in tkinter thread
        """
        if self.running:
            self.update_queue.put(callback)
    
    def show_position_window(self):
        """
        Display a PositionWindow in a tkinter frame.
        It also adds an update thread to track the position that the system decodes

        Args:
            position_window (PositionWindow): The PositionWindow instance to display
        """
        #self.position_window = position_window
            
        if not self.root:
            return
            
        # Clear existing widgets
        for widget in self.root.winfo_children():
            widget.destroy()
            
        # Create a frame for the position window
        frame = tk.Frame(self.root)
        frame.pack(fill=tk.BOTH, expand=True)
            
        # Embed the matplotlib figure in tkinter
        canvas = FigureCanvasTkAgg(self.position_window.fig, master=frame)
        self.position_window.canvas = canvas
        canvas.draw()
        canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        # Store canvas reference for updates
        self.position_window.canvas = canvas
        # Store reference to graphics module for thread-safe updates
        self.position_window._graphics_module = self
        
        # Add canvas to active_animations for automatic refreshing
        # Clear existing position window canvas if it exists
        if self.position_window.canvas in self.active_animations:
            self.active_animations.remove(self.position_window.canvas)
        self.active_animations.append(canvas)

        # Make a thread to get the position from the system
        # and update the canvas (only if not already running)
        # Check if there's already a running thread for position updates
        position_thread_running = any(
            t.is_alive() and t.name == 'position_update_thread' 
            for t in self.active_threads
        )

        # This thread will need to be stopped if a window change occurs 
        if not position_thread_running:
            thread = threading.Thread(
                target=self.position_window.update_cords_thread,
                args=(self.control_module,),
                daemon=True,
                name='position_update_thread'
            )
            thread.start()
            self.active_threads.append(thread)

    def show_main_window(self):
        """Show the main tkinter window."""
        pass  # Placeholder for now
    
    def show_stats_window(self):
        """Show the stats window."""
        pass
    
    def destroy(self):
        """Close the tkinter window (thread-safe)."""
        self.running = False
        if self.root:
            self._schedule_update(lambda: (self._on_closing if self.root else None))
            #self._schedule_update(lambda: (self.root.quit() if self.root else None))


class PositionWindow:
    def __init__(self, grid_width=10, grid_height=10, receiver_positions=None):
        """
        Initialize the PositionWindow with matplotlib.
        
        Args:
            grid_width (float): Width of the rectangular grid
            grid_height (float): Height of the rectangular grid
            receiver_positions (list of tuples, optional): List of (id, (x, y)) positions for 6 receivers.
                "id" is a 0-indexed id that denotes the tower coordinate to the tower in the real world
                The label created is index+1 to mimic real-world labels which are 1-indexed
        """
        self.grid_width = grid_width
        self.grid_height = grid_height
        self.grid_padding = 20 # Extra padding on the side to make receivers visible
        self.position_history = deque(maxlen=50)  # Store last 50 positions
        
        # Set receiver positions (6 receivers in a rectangular arrangement if not provided)
        if receiver_positions is None:
            raise ValueError("Must provide receiver positions!")
        else:
            if len(receiver_positions) != 6:
                raise ValueError("Must provide exactly 6 receiver positions")
            self.receiver_positions = receiver_positions
        
        # Initialize matplotlib figure
        self.fig, self.ax = plt.subplots(figsize=(10, 10))
        self.ax.set_xlim(-grid_width/2-self.grid_padding, grid_width/2+self.grid_padding)
        self.ax.set_ylim(-grid_height/2-self.grid_padding, grid_height/2+self.grid_padding)
        self.ax.set_aspect('equal')
        self.ax.grid(True, alpha=0.3)
        self.ax.set_xlabel('X Position')
        self.ax.set_ylabel('Y Position')
        self.ax.set_title('Vehicle Position Tracking')
        
        # Add X/Y compass rose in top left corner
        x_min, x_max = self.ax.get_xlim()
        y_min, y_max = self.ax.get_ylim()
        compass_x = 0#x_min + (x_max - x_min) * 0.1  # 10% from left edge
        compass_y = 0#y_max - (y_max - y_min) * 0.1  # 10% from top edge
        arrow_length = min((x_max - x_min), (y_max - y_min)) * 0.08  # 8% of smaller dimension
        
        # Draw X axis arrow (pointing right)
        self.ax.annotate('', xy=(compass_x + arrow_length, compass_y), 
                        xytext=(compass_x, compass_y),
                        arrowprops=dict(arrowstyle='->', color='black', lw=2, zorder=7))
        self.ax.text(compass_x + arrow_length * 0.5, compass_y - arrow_length * 0.3, 
                    'X', color='black', fontsize=12, fontweight='bold', 
                    ha='center', va='top', zorder=7)
        
        # Draw Y axis arrow (pointing up)
        self.ax.annotate('', xy=(compass_x, compass_y + arrow_length), 
                        xytext=(compass_x, compass_y),
                        arrowprops=dict(arrowstyle='->', color='black', lw=2, zorder=7))
        self.ax.text(compass_x - arrow_length * 0.3, compass_y + arrow_length * 0.5, 
                    'Y', color='black', fontsize=12, fontweight='bold', 
                    ha='right', va='center', zorder=7)
        
        # Draw receivers (red dots)
        receiver_x = [pos[1][0] for pos in self.receiver_positions]
        receiver_y = [pos[1][1] for pos in self.receiver_positions]
        self.ax.scatter(receiver_x, receiver_y, c='red', s=100, zorder=5, label='Receivers')
        
        # Add ID labels next to each receiver+1 (to match real-life labeling)
        for receiver_id, (x, y) in self.receiver_positions:
            self.ax.text(x + 5, y + 5, str(receiver_id+1), color='black', 
                        fontsize=10, fontweight='bold', zorder=6, 
                        ha='left', va='bottom')
        
        # Draw blue line connecting receivers (connect in order, then close the loop)
        # Connect receivers in a rectangular pattern
        connection_order = [0, 1, 2, 5, 4, 3, 0]  # Connect around the rectangle
        connected_x = [receiver_x[i] for i in connection_order]
        connected_y = [receiver_y[i] for i in connection_order]
        self.ax.plot(connected_x, connected_y, 'b-', linewidth=2, alpha=0.5, label='Receiver Connections')
        
        # Initialize vehicle position plot
        self.vehicle_point, = self.ax.plot([], [], 'go', markersize=10, zorder=6, label='Current Position')
        self.vehicle_trail, = self.ax.plot([], [], 'g-', linewidth=1, alpha=0.5, label='Position Trail')
        
        self.ax.legend(loc='upper right')
        plt.tight_layout()

        # Thread running variable
        self.update_thread_run = False

        # EXPERIMENTAL
        from PositionModule import StablePositionEstimator
        from PositionModule import CEPPositioning
        #self.stable_pos = StablePositionEstimator(self.receiver_positions)
        #receiver_coordinates = [cords for index,cords in receiver_positions]
        self.stable_pos = CEPPositioning(receiver_positions, min_transmitters=5)


    def update_cords(self, x, y):
        """
        Update the vehicle coordinates of the plot
        
        Args:
            x (float): X coordinate of the vehicle
            y (float): Y coordinate of the vehicle
        """
        # Add new position to history
        self.position_history.append((x, y))
        
        # Update current position (green dot)
        self.vehicle_point.set_data([x], [y])
            
        # Update position trail (green line showing last 50 positions)
        if len(self.position_history) > 1:
            trail_x = [pos[0] for pos in self.position_history]
            trail_y = [pos[1] for pos in self.position_history]
            self.vehicle_trail.set_data(trail_x, trail_y)
        
        # Note: Canvas will be automatically redrawn by _refresh_animations()
        # which runs every 10ms and calls canvas.draw() on all active_animations

    def update_cords_thread(self, control_module:ControlModule):
        """
        Thread function to update the position data asyncronously
        Must be instantiated from the graphical thread
        Continuously updates the position until the graphics module stops running
        """
        self.update_thread_run = True
        # Check if graphics module is available
        graphics_module = getattr(self, '_graphics_module', None)
        print(f"Position update thread started. Graphics module running: {graphics_module.running if graphics_module else 'None'}")

        # Instantiate the position module with the array of all receiver positions
        pos_module = PositionModule(self.receiver_positions)

        while self.update_thread_run and (graphics_module is None or graphics_module.running):
            try:
                time_start = time.time()
                # Update position with random values (replace with actual position data)
                #x, y = randint(1, 5), randint(1, 5)

                # Request the system to send a pulse and calculate the distances
                control_module.update()
                receiver_distances = control_module.get_receiver_distances()
                serial_messages = control_module.get_serial_message()
                print(f"Serial Message: {serial_messages}")
                print(f"Receiver Distances: {receiver_distances}")

                # Add positioning decode algorithm here!
                pos, result = pos_module.multilateration_method_1(receiver_distances,[0,1,2,3,4,5] ) 
                x_calc = pos[0]
                y_calc = pos[1]
                print(f"tick Pos: ({x_calc}, {y_calc})")
                
                #best_pos, best_cep, best_indices, cov, all_results = \
                #        self.stable_pos.find_best_subset(receiver_distances, max_subsets_to_try=15)
                #final_pos, final_cep, weights, used_indices = \
                #        self.stable_pos.adaptive_weighted_solution(receiver_distances)
                #print(best_pos)
                #print(final_pos)

                self.update_cords(x_calc, y_calc)
                #self.update_cords(best_pos[0],best_pos[1])
                #self.update_cords(final_pos[0],final_pos[1])
                
                print(f"~~~~~~~~~~~")
                print(time.time()-time_start) 
                # Re-check graphics module status
                graphics_module = getattr(self, '_graphics_module', None)
                #time.sleep(10)

            except Exception as e:
                print(f"Error in update_cords_thread: {e}")
                break
        
        print("Position update thread stopped")

    def close(self):
        """Close the matplotlib window."""
        plt.close(self.fig)