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
import math
import numpy as np
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
        self._queue_paused = False  # Atomic flag to pause queue processing during window changes
        self.active_threads = [] # Active threads for currently active windows
        self.active_animations = [] #FIFO list of active animations that need refreshing

        # Cached frames for fast window switching (show/hide instead of destroy/recreate)
        self._main_frame = None
        self._position_frame = None
        self._calibration_frame = None
        self._arena_maker_frame = None
        self._frames_initialized = False
        
        # Store reference to arena maker top bar button for conditional display
        self._arena_maker_top_button = None

        # Store reference to re-dimension button (shown below Back button during normal operation)
        self._arena_maker_redim_button = None

        # Store references to distance entry fields for arena maker
        self._distance_entries = {}

        # Store references to calibration point entry fields for arena maker
        self._cal_point_entries = {}

        # Instantiate control module
        self.control_module = ControlModule("127.0.0.1")

        # Instantiate the settings module
        self.settings_module = SettingsModule()

        # Instantiate matplotlib window classes
        self.position_window = PositionWindow(
                                receiver_positions=self.settings_module.get_tower_coordinates()
                                )

        self.calibration_window = CalibrationWindow(
                                receiver_positions=self.settings_module.get_tower_coordinates(),
                                settings_module=self.settings_module
                                )

        self.arena_maker_window = ArenaMakerWindow(
                                receiver_positions=self.settings_module.get_tower_coordinates()
                                )

        # Instantiate the main window (tkinter-only, no matplotlib)
        self.main_window = MainWindow()

    def start_tk_window(self):
        """ 
        Setup the tk window and necessary hooks
        """
        self.root = tk.Tk()
        self.root.title("UltraGPS Control")
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)
        # Set minimum window size to ensure visibility
        self.root.minsize(1000, 1000)
        # Ensure window is visible
        self.root.deiconify()

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
    
    def _pause_process_queue(self):
        """Pause queue processing during window changes and clear stale callbacks."""
        self._queue_paused = True
        # Clear the queue to discard stale callbacks that reference old widgets
        while not self.update_queue.empty():
            try:
                self.update_queue.get_nowait()
            except queue.Empty:
                break

    def _resume_process_queue(self):
        """Resume queue processing after window changes."""
        self._queue_paused = False

    def _process_queue(self):
        """Process update queue in the tkinter thread, every 10ms."""
        # Check if we should continue processing
        if not self.running or not self.root:
            return

        # Skip processing if queue is paused during window changes
        if not self._queue_paused:
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

        # Skip refreshing if queue is paused during window changes
        if not self._queue_paused:
            # Save current focus before drawing (canvas.draw() can steal focus)
            try:
                current_focus = self.root.focus_get()
            except Exception:
                current_focus = None

            # Refresh all active animation canvases
            for canvas in self.active_animations:
                if canvas:
                    try:
                        canvas.draw()
                    except Exception as e:
                        print(f"Error refreshing animation: {e}")

            # Restore focus if it was on an entry widget
            if current_focus is not None:
                try:
                    current_focus.focus_set()
                except Exception:
                    pass  # Widget may have been destroyed

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
        # Skip scheduling if paused during window changes to prevent stale callbacks
        if self.running and not self._queue_paused:
            self.update_queue.put(callback)

    def _hide_all_frames(self):
        """Hide all cached frames."""
        if self._main_frame:
            self._main_frame.pack_forget()
        if self._position_frame:
            self._position_frame.pack_forget()
        if self._calibration_frame:
            self._calibration_frame.pack_forget()
        if self._arena_maker_frame:
            self._arena_maker_frame.pack_forget()

    def show_position_window(self):
        """
        Display a PositionWindow in a tkinter frame.
        It also adds an update thread to track the position that the system decodes

        Args:
            position_window (PositionWindow): The PositionWindow instance to display
        """
        if not self.root:
            return

        # Pause queue processing during window change
        self._pause_process_queue()

        # Remove CalibrationWindow canvas from active_animations if present
        if self.calibration_window and hasattr(self.calibration_window, 'canvas'):
            if self.calibration_window.canvas in self.active_animations:
                self.active_animations.remove(self.calibration_window.canvas)

        # Remove ArenaMakerWindow canvas from active_animations if present
        if self.arena_maker_window and hasattr(self.arena_maker_window, 'canvas'):
            if self.arena_maker_window.canvas in self.active_animations:
                self.active_animations.remove(self.arena_maker_window.canvas)

        # Hide all frames instead of destroying
        self._hide_all_frames()

        # Update position window with current settings from settings file
        receiver_positions = self.settings_module.get_tower_coordinates()
        self.position_window.update_arena(receiver_positions)

        # If position frame already exists, just show it
        if self._position_frame:
            self._position_frame.pack(fill=tk.BOTH, expand=True)
            # Re-add canvas to active_animations
            if self.position_window.canvas not in self.active_animations:
                self.active_animations.append(self.position_window.canvas)
            # Restart position update thread if needed
            self._start_position_thread()
            self._resume_process_queue()
            return

        # First time setup - create the frame
        self._position_frame = tk.Frame(self.root, bg='black')
        self._position_frame.pack(fill=tk.BOTH, expand=True)

        # Create a top bar frame for the back button
        top_bar = tk.Frame(self._position_frame, bg='black')
        top_bar.pack(fill=tk.X, pady=5)

        # Back button in top right
        back_button = tk.Button(
            top_bar,
            text="← Back",
            command=self.show_main_window,
            bg='#39FF14',  # Neon green
            fg='black',
            font=('Arial', 12, 'bold'),
            activebackground='#2BCC10',
            activeforeground='black',
            relief=tk.RAISED,
            bd=2,
            padx=10,
            pady=5
        )
        back_button.pack(side=tk.RIGHT, padx=10)

        # Title label
        title_label = tk.Label(
            top_bar,
            text="Position Tracking",
            bg='black',
            fg='#39FF14',  # Neon green
            font=('Arial', 16, 'bold')
        )
        title_label.pack(side=tk.LEFT, padx=10)

        # Embed the matplotlib figure in tkinter
        canvas = FigureCanvasTkAgg(self.position_window.fig, master=self._position_frame)
        self.position_window.canvas = canvas
        canvas.draw_idle()  # Non-blocking draw
        canvas_widget = canvas.get_tk_widget()
        canvas_widget.configure(takefocus=False)  # Prevent canvas from stealing keyboard focus
        canvas_widget.pack(fill=tk.BOTH, expand=True)

        # Create control panel frame below the canvas
        control_frame = tk.Frame(self._position_frame, bg='black')
        control_frame.pack(fill=tk.X, pady=10)

        # Thread-safe boolean flags for showing/hiding plots (both on by default)
        self.position_window.show_multilateration = True
        self.position_window.show_cep = True

        # Callback functions to toggle the flags
        def toggle_multilateration():
            self.position_window.show_multilateration = not self.position_window.show_multilateration

        def toggle_cep():
            self.position_window.show_cep = not self.position_window.show_cep

        # Multilateration section (left side)
        multilat_frame = tk.Frame(control_frame, bg='black')
        multilat_frame.pack(side=tk.LEFT, expand=True, padx=20)

        # Multilateration position label
        self.position_window.multilat_label = tk.Label(
            multilat_frame,
            text="Multilat: (---, ---)",
            bg='black',
            fg='#39FF14',
            font=('Arial', 14, 'bold')
        )
        self.position_window.multilat_label.pack()

        # Multilateration on/off switch
        multilat_switch_frame = tk.Frame(multilat_frame, bg='black')
        multilat_switch_frame.pack(pady=5)

        # Inverted: unchecked (False) = ON (colored), checked (True) = OFF (dark)
        multilat_var = tk.BooleanVar(value=False)
        tk.Label(multilat_switch_frame, text="OFF", bg='black', fg='gray', font=('Arial', 9)).pack(side=tk.LEFT)
        multilat_switch = tk.Checkbutton(
            multilat_switch_frame,
            variable=multilat_var,
            command=toggle_multilateration,
            bg='#39FF14',           # Colored when unchecked (ON)
            fg='black',
            selectcolor='#333333',  # Dark when checked (OFF)
            activebackground='#39FF14',
            indicatoron=False,
            width=4,
            height=1,
            relief=tk.RAISED,
            bd=2
        )
        multilat_switch.pack(side=tk.LEFT, padx=5)
        tk.Label(multilat_switch_frame, text="ON", bg='black', fg='#39FF14', font=('Arial', 9, 'bold')).pack(side=tk.LEFT)

        # Insufficient Receivers section (center)
        insuff_frame = tk.Frame(control_frame, bg='black')
        insuff_frame.pack(side=tk.LEFT, expand=True, padx=20)

        # Frame to hold label and LED side by side
        insuff_label_frame = tk.Frame(insuff_frame, bg='black')
        insuff_label_frame.pack()

        # Insufficient receivers label
        tk.Label(
            insuff_label_frame,
            text="Insufficient Receivers:",
            bg='black',
            fg='white',
            font=('Arial', 12, 'bold')
        ).pack(side=tk.LEFT)

        # Insufficient receivers LED indicator (green = sufficient, red = insufficient)
        insuff_led_canvas = tk.Canvas(insuff_label_frame, width=20, height=20, bg='black', highlightthickness=0)
        insuff_led_canvas.pack(side=tk.LEFT, padx=5)
        # Draw LED circle (initially green - sufficient receivers)
        self.position_window.insuff_receivers_led = insuff_led_canvas.create_oval(2, 2, 18, 18, fill='#39FF14', outline='white', width=2)
        self.position_window.insuff_led_canvas = insuff_led_canvas

        # CEP section (right side)
        cep_frame = tk.Frame(control_frame, bg='black')
        cep_frame.pack(side=tk.LEFT, expand=True, padx=20)

        # Frame to hold CEP label and validity LED side by side
        cep_label_frame = tk.Frame(cep_frame, bg='black')
        cep_label_frame.pack()

        # CEP position label
        self.position_window.cep_label = tk.Label(
            cep_label_frame,
            text="CEP: (---, ---)",
            bg='black',
            fg='#FFFF00',
            font=('Arial', 14, 'bold')
        )
        self.position_window.cep_label.pack(side=tk.LEFT)

        # CEP validity LED indicator (green = valid, red = invalid)
        cep_led_canvas = tk.Canvas(cep_label_frame, width=20, height=20, bg='black', highlightthickness=0)
        cep_led_canvas.pack(side=tk.LEFT, padx=5)
        # Draw LED circle (initially green)
        self.position_window.cep_validity_led = cep_led_canvas.create_oval(2, 2, 18, 18, fill='#39FF14', outline='white', width=2)
        self.position_window.cep_led_canvas = cep_led_canvas

        # CEP on/off switch
        cep_switch_frame = tk.Frame(cep_frame, bg='black')
        cep_switch_frame.pack(pady=5)

        # Inverted: unchecked (False) = ON (colored), checked (True) = OFF (dark)
        cep_var = tk.BooleanVar(value=False)
        tk.Label(cep_switch_frame, text="OFF", bg='black', fg='gray', font=('Arial', 9)).pack(side=tk.LEFT)
        cep_switch = tk.Checkbutton(
            cep_switch_frame,
            variable=cep_var,
            command=toggle_cep,
            bg='#FFFF00',           # Colored when unchecked (ON)
            fg='black',
            selectcolor='#333333',  # Dark when checked (OFF)
            activebackground='#FFFF00',
            indicatoron=False,
            width=4,
            height=1,
            relief=tk.RAISED,
            bd=2
        )
        cep_switch.pack(side=tk.LEFT, padx=5)
        tk.Label(cep_switch_frame, text="ON", bg='black', fg='#FFFF00', font=('Arial', 9, 'bold')).pack(side=tk.LEFT)

        # Calibration button section (far right)
        calib_frame = tk.Frame(control_frame, bg='black')
        calib_frame.pack(side=tk.LEFT, expand=True, padx=20)

        calib_button = tk.Button(
            calib_frame,
            text="Calibration",
            command=self.show_calibration_window,
            bg='#FF00FF',  # Neon magenta
            fg='white',
            font=('Arial', 12, 'bold'),
            activebackground='#CC00CC',
            activeforeground='white',
            relief=tk.RAISED,
            bd=2,
            padx=10,
            pady=5
        )
        calib_button.pack()

        # Arena Maker button section (far right)
        arena_frame = tk.Frame(control_frame, bg='black')
        arena_frame.pack(side=tk.LEFT, expand=True, padx=20)

        arena_button = tk.Button(
            arena_frame,
            text="Arena Maker",
            command=self.show_arena_maker_window,
            bg='#00FFFF',  # Neon cyan
            fg='black',
            font=('Arial', 12, 'bold'),
            activebackground='#00CCCC',
            activeforeground='black',
            relief=tk.RAISED,
            bd=2,
            padx=10,
            pady=5
        )
        arena_button.pack()

        # Store canvas reference for updates
        self.position_window.canvas = canvas
        # Store reference to graphics module for thread-safe updates
        self.position_window._graphics_module = self
        
        # Add canvas to active_animations for automatic refreshing
        # Clear existing position window canvas if it exists
        if self.position_window.canvas in self.active_animations:
            self.active_animations.remove(self.position_window.canvas)
        self.active_animations.append(canvas)

        # Start position update thread
        self._start_position_thread()

        # Resume queue processing after window change is complete
        self._resume_process_queue()

    def _start_position_thread(self):
        """Start the position update thread if not already running."""
        position_thread_running = any(
            t.is_alive() and t.name == 'position_update_thread'
            for t in self.active_threads
        )

        if not position_thread_running:
            thread = threading.Thread(
                target=self.position_window.update_cords_thread,
                args=(self.control_module,),
                daemon=True,
                name='position_update_thread'
            )
            thread.start()
            self.active_threads.append(thread)

    def show_calibration_window(self):
        """
        Display the CalibrationWindow in a tkinter frame.
        Frees resources from PositionWindow and shows a simplified calibration view.
        """
        if not self.root:
            return

        # Pause queue processing during window change
        self._pause_process_queue()

        # Stop the position update thread
        if self.position_window:
            self.position_window.update_thread_run = False

        # Remove PositionWindow canvas from active_animations
        if self.position_window and hasattr(self.position_window, 'canvas'):
            if self.position_window.canvas in self.active_animations:
                self.active_animations.remove(self.position_window.canvas)

        # Hide all frames instead of destroying
        self._hide_all_frames()

        # Update calibration window with current settings from settings file
        receiver_positions = self.settings_module.get_tower_coordinates()
        self.calibration_window.update_arena(receiver_positions)

        # If calibration frame already exists, just show it
        if self._calibration_frame:
            self._calibration_frame.pack(fill=tk.BOTH, expand=True)
            # Re-add canvas to active_animations
            if self.calibration_window.canvas not in self.active_animations:
                self.active_animations.append(self.calibration_window.canvas)
            # Update offset display with current values
            self._update_offset_display()
            self._resume_process_queue()
            return

        # First time setup - create the frame
        self._calibration_frame = tk.Frame(self.root, bg='black')
        self._calibration_frame.pack(fill=tk.BOTH, expand=True)

        # Create a top bar frame for the back button and start calibration button
        top_bar = tk.Frame(self._calibration_frame, bg='black')
        top_bar.pack(fill=tk.X, pady=5)

        # Back button in top right
        back_button = tk.Button(
            top_bar,
            text="← Back",
            command=self.show_main_window,
            bg='#39FF14',  # Neon green
            fg='black',
            font=('Arial', 12, 'bold'),
            activebackground='#2BCC10',
            activeforeground='black',
            relief=tk.RAISED,
            bd=2,
            padx=10,
            pady=5
        )
        back_button.pack(side=tk.RIGHT, padx=10)

        # Title label
        title_label = tk.Label(
            top_bar,
            text="Calibration Mode",
            bg='black',
            fg='#FF00FF',  # Neon magenta
            font=('Arial', 16, 'bold')
        )
        title_label.pack(side=tk.LEFT, padx=10)

        # Start Calibration button (centered)
        start_cal_button = tk.Button(
            top_bar,
            text="Start Calibration",
            command=self._start_calibration,
            bg='#FFD700',  # Gold
            fg='black',
            font=('Arial', 12, 'bold'),
            activebackground='#FFC000',
            activeforeground='black',
            relief=tk.RAISED,
            bd=2,
            padx=15,
            pady=5
        )
        start_cal_button.pack(side=tk.LEFT, padx=50)

        # Create main content frame (canvas + offset panel)
        content_frame = tk.Frame(self._calibration_frame, bg='black')
        content_frame.pack(fill=tk.BOTH, expand=True)

        # Embed the matplotlib figure in tkinter
        canvas = FigureCanvasTkAgg(self.calibration_window.fig, master=content_frame)
        self.calibration_window.canvas = canvas
        canvas.draw_idle()  # Non-blocking draw
        canvas_widget = canvas.get_tk_widget()
        canvas_widget.configure(takefocus=False)  # Prevent canvas from stealing keyboard focus
        canvas_widget.pack(fill=tk.BOTH, expand=True)

        # Create Ax + b Receiver Offsets panel below the canvas
        offset_panel = tk.Frame(self._calibration_frame, bg='#222222', relief=tk.RAISED, bd=2)
        offset_panel.pack(fill=tk.X, pady=5, padx=10)

        # Offset panel title
        offset_title = tk.Label(
            offset_panel,
            text="Ax + b Receiver Offsets",
            bg='#222222',
            fg='#FFD700',  # Gold
            font=('Arial', 12, 'bold')
        )
        offset_title.pack(pady=5)

        # Create offset labels frame with 2 columns
        offset_labels_frame = tk.Frame(offset_panel, bg='#222222')
        offset_labels_frame.pack(fill=tk.X, padx=10, pady=5)

        # Store offset labels for updating
        self._offset_labels = {}

        # Left column: Receivers 3, 2, 1 (from top to bottom)
        left_column = tk.Frame(offset_labels_frame, bg='#222222')
        left_column.pack(side=tk.LEFT, expand=True, fill=tk.X)

        for recv_id in [2, 1, 0]:  # Receivers 3, 2, 1
            label = tk.Label(
                left_column,
                text=f"Receiver {recv_id + 1} Offset: 0.0x + 0.0",
                bg='#222222',
                fg='white',
                font=('Arial', 10)
            )
            label.pack(anchor='w', pady=2)
            self._offset_labels[recv_id] = label

        # Right column: Receivers 6, 5, 4 (from top to bottom)
        right_column = tk.Frame(offset_labels_frame, bg='#222222')
        right_column.pack(side=tk.RIGHT, expand=True, fill=tk.X)

        for recv_id in [5, 4, 3]:  # Receivers 6, 5, 4
            label = tk.Label(
                right_column,
                text=f"Receiver {recv_id + 1} Offset: 0.0x + 0.0",
                bg='#222222',
                fg='white',
                font=('Arial', 10)
            )
            label.pack(anchor='w', pady=2)
            self._offset_labels[recv_id] = label

        # Update offset display with current values from settings
        self._update_offset_display()

        # Store reference to graphics module for thread-safe updates
        self.calibration_window._graphics_module = self

        # Add canvas to active_animations for automatic refreshing
        if hasattr(self.calibration_window, 'canvas') and self.calibration_window.canvas in self.active_animations:
            self.active_animations.remove(self.calibration_window.canvas)
        self.active_animations.append(canvas)

        # Resume queue processing after window change is complete
        self._resume_process_queue()

    def _update_offset_display(self):
        """Update the offset display labels with current values from settings."""
        if not hasattr(self, '_offset_labels'):
            return

        for recv_id in range(6):
            if recv_id in self._offset_labels:
                offset = self.settings_module.get_receiver_offset(recv_id)
                if offset:
                    slope = offset.get('slope', 0.0)
                    intercept = offset.get('intercept', 0.0)
                    self._offset_labels[recv_id].config(
                        text=f"Receiver {recv_id + 1} Offset: {slope:.4f}x + {intercept:.4f}"
                    )

    def _start_calibration(self):
        """Start the calibration process with popup dialogs."""
        # Get calibration point 1 coordinates
        cal_point_1 = self.settings_module.cal_point_1
        min_reads = self.settings_module.calibration_reads

        # Show calibration point 1 on arena
        self.calibration_window.show_calibration_point(cal_point_1, 1)

        # Pause queue processing for dialog
        self._pause_process_queue()

        # Create popup dialog
        dialog = tk.Toplevel(self.root)
        dialog.title("Calibration - Run 1")
        dialog.geometry("400x200")
        dialog.configure(bg='black')
        dialog.transient(self.root)
        try:
            dialog.grab_set()
        except tk.TclError:
            pass  # Grab failed, continue without it
        dialog.focus_force()

        # Center the dialog
        dialog.geometry("+%d+%d" % (self.root.winfo_rootx() + 200, self.root.winfo_rooty() + 200))

        # Instructions
        instructions = tk.Label(
            dialog,
            text=f"Place the transmitter at Calibration Point 1\n\nCoordinates: ({cal_point_1[0]:.1f}, {cal_point_1[1]:.1f})\n\nMinimum reads required: {min_reads}",
            bg='black',
            fg='#00FFFF',
            font=('Arial', 12),
            justify=tk.CENTER
        )
        instructions.pack(pady=20)

        # Button frame
        button_frame = tk.Frame(dialog, bg='black')
        button_frame.pack(pady=20)

        result = {'ready': False}

        def on_ready():
            result['ready'] = True
            dialog.destroy()

        def on_cancel():
            result['ready'] = False
            dialog.destroy()

        ready_button = tk.Button(
            button_frame,
            text="Ready",
            command=on_ready,
            bg='#39FF14',
            fg='black',
            font=('Arial', 12, 'bold'),
            padx=20,
            pady=5
        )
        ready_button.pack(side=tk.LEFT, padx=20)

        cancel_button = tk.Button(
            button_frame,
            text="Cancel",
            command=on_cancel,
            bg='#FF4444',
            fg='white',
            font=('Arial', 12, 'bold'),
            padx=20,
            pady=5
        )
        cancel_button.pack(side=tk.LEFT, padx=20)

        dialog.wait_window()

        self._resume_process_queue()

        if not result['ready']:
            # User cancelled
            self.calibration_window.hide_calibration_point()
            return

        # Clear run 1 data
        self.calibration_window.clear_histogram_data(1)

        # Start calibration run 1
        self._run_calibration(1, min_reads)

    def _run_calibration(self, run_num, min_reads):
        """
        Run the calibration process for a specific run.

        Args:
            run_num: 1 or 2 for which calibration run
            min_reads: Minimum number of reads required for each receiver
        """
        self.calibration_window.calibration_running = True
        self.calibration_window.current_run = run_num

        # Start calibration thread
        cal_thread = threading.Thread(
            target=self._calibration_thread,
            args=(run_num, min_reads),
            daemon=True,
            name=f'calibration_run_{run_num}'
        )
        cal_thread.start()
        self.active_threads.append(cal_thread)

    def _calibration_thread(self, run_num, min_reads):
        """
        Thread function for calibration run.
        Collects serial messages until min_reads threshold is met for all receivers.
        """
        # Initial read to clear network
        try:
            self.control_module.update()
            _ = self.control_module.get_serial_message()
        except Exception as e:
            print(f"Error clearing network: {e}")

        # Track when to update histograms (every N reads instead of every read)
        read_count = 0
        histogram_update_interval = 5  # Update histograms every 5 reads

        # Keep reading until all receivers have met min_reads threshold
        while self.calibration_window.calibration_running:
            try:
                # Request new reading
                self.control_module.update()
                serial_msg = self.control_module.get_serial_message()

                # Parse serial message - assume space-separated values, one per receiver
                try:
                    serial_values = serial_msg.strip().split()
                    if len(serial_values) >= 6:
                        for recv_id in range(6):
                            try:
                                value = float(serial_values[recv_id])
                                self.calibration_window.add_serial_reading(recv_id, value, run_num)
                            except (ValueError, IndexError):
                                pass
                        read_count += 1
                except Exception as e:
                    print(f"Error parsing serial message: {e}")
                    continue

                # Update histograms periodically instead of every read
                if read_count % histogram_update_interval == 0:
                    self.root.after(0, lambda n=run_num: self._update_all_histograms(n))

                # Check if all receivers have met min_reads threshold
                all_met = True
                for recv_id in range(6):
                    if self.calibration_window.get_max_count(recv_id, run_num) < min_reads:
                        all_met = False
                        break

                if all_met:
                    break

            except Exception as e:
                print(f"Error in calibration thread: {e}")
                break

        # Final histogram update
        self.root.after(0, lambda n=run_num: self._update_all_histograms(n))

        self.calibration_window.calibration_running = False
        # Schedule next step on main thread
        self.root.after(0, lambda: self._calibration_run_complete(run_num, min_reads))

    def _update_all_histograms(self, run_num):
        """Update all histograms for a given run."""
        for recv_id in range(6):
            self.calibration_window.update_histogram(recv_id, run_num)


    def _calibration_run_complete(self, run_num, min_reads):
        """Called when a calibration run is complete."""
        if run_num == 1:
            # Run 1 complete, start Run 2
            cal_point_2 = self.settings_module.cal_point_2

            # Show calibration point 2 on arena
            self.calibration_window.show_calibration_point(cal_point_2, 2)

            # Pause queue processing for dialog
            self._pause_process_queue()

            # Create popup dialog for Run 2
            dialog = tk.Toplevel(self.root)
            dialog.title("Calibration - Run 2")
            dialog.geometry("400x200")
            dialog.configure(bg='black')
            dialog.transient(self.root)
            try:
                dialog.grab_set()
            except tk.TclError:
                pass  # Grab failed, continue without it
            dialog.focus_force()

            dialog.geometry("+%d+%d" % (self.root.winfo_rootx() + 200, self.root.winfo_rooty() + 200))

            instructions = tk.Label(
                dialog,
                text=f"Run 1 Complete!\n\nPlace the transmitter at Calibration Point 2\n\nCoordinates: ({cal_point_2[0]:.1f}, {cal_point_2[1]:.1f})",
                bg='black',
                fg='#00FFFF',
                font=('Arial', 12),
                justify=tk.CENTER
            )
            instructions.pack(pady=20)

            button_frame = tk.Frame(dialog, bg='black')
            button_frame.pack(pady=20)

            result = {'ready': False}

            def on_ready():
                result['ready'] = True
                dialog.destroy()

            def on_cancel():
                result['ready'] = False
                dialog.destroy()

            ready_button = tk.Button(
                button_frame,
                text="Ready",
                command=on_ready,
                bg='#39FF14',
                fg='black',
                font=('Arial', 12, 'bold'),
                padx=20,
                pady=5
            )
            ready_button.pack(side=tk.LEFT, padx=20)

            cancel_button = tk.Button(
                button_frame,
                text="Cancel",
                command=on_cancel,
                bg='#FF4444',
                fg='white',
                font=('Arial', 12, 'bold'),
                padx=20,
                pady=5
            )
            cancel_button.pack(side=tk.LEFT, padx=20)

            dialog.wait_window()

            self._resume_process_queue()

            if not result['ready']:
                self.calibration_window.hide_calibration_point()
                return

            # Clear run 2 data
            self.calibration_window.clear_histogram_data(2)

            # Start calibration run 2
            self._run_calibration(2, min_reads)

        else:
            # Run 2 complete, calculate offsets
            self.calibration_window.hide_calibration_point()
            self._calculate_and_save_offsets()

    def _calculate_and_save_offsets(self):
        """
        Calculate and save the offset values for all receivers.
        a = (known_dist_1 - known_dist_2) / (valid_count_1 - valid_count_2)
        b = known_dist_1 - (a * valid_count_1)
        """
        for recv_id in range(6):
            # Get known distances from settings
            cal_distances = self.settings_module.get_receiver_cal_distances(recv_id)
            if not cal_distances or len(cal_distances) < 2:
                print(f"Missing calibration distances for receiver {recv_id}")
                continue

            known_dist_1 = cal_distances[0]  # Distance to cal_point_1
            known_dist_2 = cal_distances[1]  # Distance to cal_point_2

            # Get most frequent serial values from each run
            valid_count_1 = self.calibration_window.get_most_frequent_value(recv_id, 1)
            valid_count_2 = self.calibration_window.get_most_frequent_value(recv_id, 2)

            if valid_count_1 is None or valid_count_2 is None:
                print(f"Missing calibration data for receiver {recv_id}")
                continue

            # Calculate slope (a) and intercept (b)
            try:
                if valid_count_1 == valid_count_2:
                    print(f"Warning: Same serial value for both runs on receiver {recv_id}")
                    continue

                a = (known_dist_1 - known_dist_2) / (valid_count_1 - valid_count_2)
                b = known_dist_1 - (a * valid_count_1)

                # Save to settings (note: per task doc, slope=a, intercept=b)
                self.settings_module.set_receiver_offset(recv_id, a, b)

                print(f"Receiver {recv_id + 1}: a={a:.4f}, b={b:.4f}")

            except Exception as e:
                print(f"Error calculating offset for receiver {recv_id}: {e}")

        # Update the offset display
        self._update_offset_display()

        # Show completion dialog with a small delay to allow previous grabs to release
        self.root.after(100, self._show_calibration_complete_dialog)

    def _show_calibration_complete_dialog(self):
        """Show the calibration complete dialog."""
        self._pause_process_queue()

        dialog = tk.Toplevel(self.root)
        dialog.title("Calibration Complete")
        dialog.geometry("300x150")
        dialog.configure(bg='black')
        dialog.transient(self.root)

        # Try to grab, but don't fail if another grab is active
        try:
            dialog.grab_set()
        except tk.TclError:
            pass  # Grab failed, continue without it

        dialog.focus_force()

        dialog.geometry("+%d+%d" % (self.root.winfo_rootx() + 300, self.root.winfo_rooty() + 250))

        msg = tk.Label(
            dialog,
            text="Calibration Complete!\n\nOffset values have been calculated\nand saved to the configuration file.",
            bg='black',
            fg='#39FF14',
            font=('Arial', 12),
            justify=tk.CENTER
        )
        msg.pack(pady=30)

        ok_button = tk.Button(
            dialog,
            text="OK",
            command=dialog.destroy,
            bg='#39FF14',
            fg='black',
            font=('Arial', 12, 'bold'),
            padx=30,
            pady=5
        )
        ok_button.pack()

        dialog.wait_window()
        self._resume_process_queue()

    def _calculate_receiver_positions_from_dimensions(self, width, height):
        """
        Calculate receiver positions in a rectangular pattern based on arena dimensions.
        Pattern: Left side (top to bottom) = Receivers 3, 2, 1
                 Right side (top to bottom) = Receivers 6, 5, 4

        Args:
            width (float): Arena width in cm
            height (float): Arena height in cm

        Returns:
            list of tuples: [(id, (x, y)), ...] for 6 receivers
        """
        # Left side (top to bottom): Receivers 3, 2, 1 (ids 2, 1, 0)
        # Right side (top to bottom): Receivers 6, 5, 4 (ids 5, 4, 3)

        half_width = width / 2.0
        half_height = height / 2.0

        positions = [
            (0, (-half_width, -half_height)),   # Receiver 1 (id 0): Left side, bottom
            (1, (-half_width, 0)),             # Receiver 2 (id 1): Left side, middle
            (2, (-half_width, half_height)),   # Receiver 3 (id 2): Left side, top
            (3, (half_width, -half_height)),   # Receiver 4 (id 3): Right side, bottom
            (4, (half_width, 0)),              # Receiver 5 (id 4): Right side, middle
            (5, (half_width, half_height))     # Receiver 6 (id 5): Right side, top
        ]

        return positions

    def _calculate_positions_from_distances(self, r1, r2, r3, r4, r5, r6, r7=None, r8=None, r9=0, r10=0):
        """
        Calculate receiver positions from inter-receiver distances.

        Receivers 2 and 5 are at y = r9 and y = r10 respectively (default 0).
        R1, R2, R4, R5 only affect the y-axis (vertical distances).
        R3, R6 only affect the x-axis (horizontal distances, symmetric across y-axis).
        R7, R8 control x-distance from origin to receivers 2 and 5.
        R9, R10 control y-offset from origin for receivers 2 and 5.

        Distance definitions:
        - R1: vertical distance from receiver 2 to receiver 3 (y of receiver 3 relative to receiver 2)
        - R2: vertical distance from receiver 2 to receiver 1 (y of receiver 1 relative to receiver 2)
        - R3: horizontal distance from receiver 1 to receiver 4 (bottom width)
        - R4: vertical distance from receiver 5 to receiver 4 (y of receiver 4 relative to receiver 5)
        - R5: vertical distance from receiver 5 to receiver 6 (y of receiver 6 relative to receiver 5)
        - R6: horizontal distance from receiver 3 to receiver 6 (top width)
        - R7: x-distance from origin to receiver 2 (defaults to R6/2 if None)
        - R8: x-distance from origin to receiver 5 (defaults to R6/2 if None)
        - R9: y-offset of receiver 2 from origin (default 0)
        - R10: y-offset of receiver 5 from origin (default 0)

        Layout (when R9=R10=0):
          3 ----R6---- 6
          |            |
          R1          R5
          |            |
     --R7-2            5-R8--  <- y = 0, origin in center
          |            |
          R2          R4
          |            |
          1 ----R3---- 4

        Args:
            r1-r6: Distances in cm
            r7: X-distance from origin to receiver 2 (optional, defaults to r6/2)
            r8: X-distance from origin to receiver 5 (optional, defaults to r6/2)
            r9: Y-offset of receiver 2 from origin (default 0)
            r10: Y-offset of receiver 5 from origin (default 0)

        Returns:
            list: positions as [(id, (x, y)), ...]
        """
        # Default R7 and R8 to R6/2 if not specified
        if r7 is None:
            r7 = r6 / 2.0
        if r8 is None:
            r8 = r6 / 2.0

        # X positions
        x3 = -r6 / 2.0   # Receiver 3: left side, determined by R6
        x2 = -r7         # Receiver 2: determined by R7 (distance from origin)
        x1 = -r3 / 2.0   # Receiver 1: determined by R3
        x4 = r3 / 2.0    # Receiver 4: symmetric to receiver 1
        x5 = r8          # Receiver 5: determined by R8 (distance from origin)
        x6 = r6 / 2.0    # Receiver 6: right side, determined by R6

        # Y positions (receivers 2 and 5 offset by R9 and R10)
        y2 = r9          # Receiver 2: y-offset from origin
        y5 = r10         # Receiver 5: y-offset from origin
        y3 = y2 + r1     # Receiver 3: R1 above receiver 2
        y1 = y2 - r2     # Receiver 1: R2 below receiver 2
        y6 = y5 + r5     # Receiver 6: R5 above receiver 5
        y4 = y5 - r4     # Receiver 4: R4 below receiver 5

        positions = [
            (0, (x1, y1)),   # Receiver 1 (id 0): bottom left
            (1, (x2, y2)),   # Receiver 2 (id 1): middle left
            (2, (x3, y3)),   # Receiver 3 (id 2): top left
            (3, (x4, y4)),   # Receiver 4 (id 3): bottom right
            (4, (x5, y5)),   # Receiver 5 (id 4): middle right
            (5, (x6, y6))    # Receiver 6 (id 5): top right
        ]

        return positions

    def _calculate_distances_from_positions(self, positions):
        """
        Calculate inter-receiver distances from positions.

        Returns vertical distances for R1, R2, R4, R5, horizontal distances for R3, R6,
        origin-to-receiver distances for R7, R8, and y-offsets for R9, R10.

        Args:
            positions: List of (id, (x, y)) tuples for 6 receivers

        Returns:
            tuple: (r1, r2, r3, r4, r5, r6, r7, r8, r9, r10) distances
        """
        # Create a dict for easy lookup by id
        pos_dict = {p[0]: p[1] for p in positions}

        # Vertical distances (y-axis only)
        r1 = abs(pos_dict[2][1] - pos_dict[1][1])  # Receiver 3 to 2 (y difference)
        r2 = abs(pos_dict[1][1] - pos_dict[0][1])  # Receiver 2 to 1 (y difference)
        r4 = abs(pos_dict[4][1] - pos_dict[3][1])  # Receiver 5 to 4 (y difference)
        r5 = abs(pos_dict[5][1] - pos_dict[4][1])  # Receiver 6 to 5 (y difference)

        # Horizontal distances (x-axis only)
        r3 = abs(pos_dict[3][0] - pos_dict[0][0])  # Receiver 4 to 1 (x difference)
        r6 = abs(pos_dict[5][0] - pos_dict[2][0])  # Receiver 6 to 3 (x difference)

        # Origin-to-receiver distances (x-distance from origin)
        r7 = abs(pos_dict[1][0])  # Receiver 2 x-distance from origin
        r8 = abs(pos_dict[4][0])  # Receiver 5 x-distance from origin

        # Y-offsets from origin (can be positive or negative)
        r9 = pos_dict[1][1]   # Receiver 2 y-offset from origin
        r10 = pos_dict[4][1]  # Receiver 5 y-offset from origin

        return r1, r2, r3, r4, r5, r6, r7, r8, r9, r10

    def _update_arena_from_distances(self):
        """Update arena positions when distance entries change."""
        try:
            # Get values from entry fields
            r1 = float(self._distance_entries['R1'].get())
            r2 = float(self._distance_entries['R2'].get())
            r3 = float(self._distance_entries['R3'].get())
            r4 = float(self._distance_entries['R4'].get())
            r5 = float(self._distance_entries['R5'].get())
            r6 = float(self._distance_entries['R6'].get())
            r7 = float(self._distance_entries['R7'].get())
            r8 = float(self._distance_entries['R8'].get())
            r9 = float(self._distance_entries['R9'].get())
            r10 = float(self._distance_entries['R10'].get())

            # Initialize previous R9/R10 tracking if not exists
            if not hasattr(self, '_prev_r9'):
                self._prev_r9 = 0.0
            if not hasattr(self, '_prev_r10'):
                self._prev_r10 = 0.0

            prev_r9 = self._prev_r9
            prev_r10 = self._prev_r10

            # Handle R9 changes - adjust both R1 and R2 to keep receivers 1 and 3 in place
            # When R9 > 0: add R9 to R2, subtract R9 from R1
            # When R9 < 0: subtract R9 from R2, add R9 to R1
            if r9 != prev_r9:
                # Restore previous adjustment
                if prev_r9 > 0:
                    r2 = r2 - prev_r9  # Remove what was added to R2
                    r1 = r1 + prev_r9  # Remove what was subtracted from R1
                elif prev_r9 < 0:
                    r2 = r2 - prev_r9  # Remove what was subtracted from R2 (add negative)
                    r1 = r1 + prev_r9  # Remove what was added to R1 (subtract negative)

                # Apply new adjustment
                if r9 > 0:
                    r2 = r2 + r9  # Add R9 to R2
                    r1 = r1 - r9  # Subtract R9 from R1
                elif r9 < 0:
                    r2 = r2 + r9  # Add R9 to R2 (because of negative r2)
                    r1 = r1 - r9  # Substract R1 and R9

                # Update entry fields
                self._distance_entries['R1'].delete(0, 'end')
                self._distance_entries['R1'].insert(0, f"{r1:.1f}")
                self._distance_entries['R2'].delete(0, 'end')
                self._distance_entries['R2'].insert(0, f"{r2:.1f}")

                self._prev_r9 = r9

            # Handle R10 changes - adjust both R4 and R5 to keep receivers 4 and 6 in place
            # When R10 > 0: add R10 to R4, subtract R10 from R5
            # When R10 < 0: subtract R10 from R4, add R10 to R5
            if r10 != prev_r10:
                # Restore previous adjustment
                if prev_r10 > 0:
                    r4 = r4 - prev_r10  # Remove what was added to R4
                    r5 = r5 + prev_r10  # Remove what was subtracted from R5
                elif prev_r10 < 0:
                    r4 = r4 - prev_r10  # Remove what was subtracted from R4 (add negative)
                    r5 = r5 + prev_r10  # Remove what was added to R5 (subtract negative)

                # Apply new adjustment
                if r10 > 0:
                    r4 = r4 + r10  # Add R10 to R4
                    r5 = r5 - r10  # Subtract R10 from R5
                elif r10 < 0:
                    r4 = r4 + r10  # Subtract R10 from R4 (subtracting negative = add)
                    r5 = r5 - r10  # Add R10 to R5 (adding negative = subtract)

                # Update entry fields
                self._distance_entries['R4'].delete(0, 'end')
                self._distance_entries['R4'].insert(0, f"{r4:.1f}")
                self._distance_entries['R5'].delete(0, 'end')
                self._distance_entries['R5'].insert(0, f"{r5:.1f}")

                self._prev_r10 = r10

            # Validate positive values for R1-R8 (R9, R10 can be any value including 0 or negative)
            if any(v <= 0 for v in [r1, r2, r3, r4, r5, r6, r7, r8]):
                return  # Invalid input, don't update

            # Calculate new positions
            new_positions = self._calculate_positions_from_distances(r1, r2, r3, r4, r5, r6, r7, r8, r9, r10)

            # Update arena maker window
            self.arena_maker_window.update_receiver_positions(new_positions)

            # Update settings module
            for receiver_id, (x, y) in new_positions:
                self.settings_module.set_receiver_position(receiver_id, x, y)

            # Recalculate calibration distances for all receivers
            cal_p1 = self.settings_module.cal_point_1
            cal_p2 = self.settings_module.cal_point_2
            for receiver_id, (rx, ry) in new_positions:
                dist_1 = math.sqrt((rx - cal_p1[0])**2 + (ry - cal_p1[1])**2)
                dist_2 = math.sqrt((rx - cal_p2[0])**2 + (ry - cal_p2[1])**2)
                self.settings_module.set_receiver_cal_distances(receiver_id, [dist_1, dist_2])

        except ValueError:
            pass  # Invalid input, don't update

    def _update_calibration_points_from_entries(self):
        """Update calibration points when coordinate entries change."""
        if not hasattr(self, '_cal_point_entries') or not self._cal_point_entries:
            return

        try:
            # Get values from entry fields
            cal1_x = float(self._cal_point_entries['Cal 1']['x'].get())
            cal1_y = float(self._cal_point_entries['Cal 1']['y'].get())
            cal2_x = float(self._cal_point_entries['Cal 2']['x'].get())
            cal2_y = float(self._cal_point_entries['Cal 2']['y'].get())

            # Update settings module
            self.settings_module.cal_point_1 = [cal1_x, cal1_y]
            self.settings_module.cal_point_2 = [cal2_x, cal2_y]

            # Update calibration points on the arena plot
            if hasattr(self.arena_maker_window, 'update_calibration_points'):
                self.arena_maker_window.update_calibration_points(
                    self.settings_module.cal_point_1,
                    self.settings_module.cal_point_2
                )

            # Recalculate calibration distances for all receivers
            cal_p1 = self.settings_module.cal_point_1
            cal_p2 = self.settings_module.cal_point_2
            for receiver_id in range(6):
                pos = self.settings_module.get_receiver_position(receiver_id)
                if pos:
                    rx, ry = pos
                    dist_1 = math.sqrt((rx - cal_p1[0])**2 + (ry - cal_p1[1])**2)
                    dist_2 = math.sqrt((rx - cal_p2[0])**2 + (ry - cal_p2[1])**2)
                    self.settings_module.set_receiver_cal_distances(receiver_id, [dist_1, dist_2])

            # Update RC entries to reflect new distances
            if 'RC_1' in self._cal_point_entries:
                rc1_dist = math.sqrt(cal1_x**2 + cal1_y**2)
                self._cal_point_entries['RC_1'].delete(0, tk.END)
                self._cal_point_entries['RC_1'].insert(0, f"{rc1_dist:.1f}")
            if 'RC_2' in self._cal_point_entries:
                rc2_dist = math.sqrt(cal2_x**2 + cal2_y**2)
                self._cal_point_entries['RC_2'].delete(0, tk.END)
                self._cal_point_entries['RC_2'].insert(0, f"{rc2_dist:.1f}")

        except ValueError:
            pass  # Invalid input, don't update

    def _update_rc_from_entries(self):
        """Update calibration points when RC_1 or RC_2 entries change.

        RC represents the Euclidean distance from the origin to the calibration point.
        When RC changes, both X and Y are scaled proportionally to maintain the same
        angle from the origin while achieving the new distance.
        """
        if not hasattr(self, '_cal_point_entries') or not self._cal_point_entries:
            return

        try:
            # Get current calibration points
            cal_p1 = list(self.settings_module.cal_point_1)
            cal_p2 = list(self.settings_module.cal_point_2)

            # Get RC values from entries
            rc1_val = float(self._cal_point_entries['RC_1'].get())
            rc2_val = float(self._cal_point_entries['RC_2'].get())

            # Update cal_point_1 based on RC_1
            # Scale both X and Y to maintain same angle while achieving new distance
            current_dist_1 = math.sqrt(cal_p1[0]**2 + cal_p1[1]**2)
            if current_dist_1 > 0.001:  # Avoid division by zero
                scale_1 = rc1_val / current_dist_1
                cal_p1[0] = cal_p1[0] * scale_1
                cal_p1[1] = cal_p1[1] * scale_1
            else:
                # If current distance is ~0, default to positive Y-axis
                cal_p1[0] = 0.0
                cal_p1[1] = rc1_val

            # Update cal_point_2 based on RC_2
            current_dist_2 = math.sqrt(cal_p2[0]**2 + cal_p2[1]**2)
            if current_dist_2 > 0.001:  # Avoid division by zero
                scale_2 = rc2_val / current_dist_2
                cal_p2[0] = cal_p2[0] * scale_2
                cal_p2[1] = cal_p2[1] * scale_2
            else:
                # If current distance is ~0, default to negative Y-axis
                cal_p2[0] = 0.0
                cal_p2[1] = -rc2_val

            # Update settings module
            self.settings_module.cal_point_1 = cal_p1
            self.settings_module.cal_point_2 = cal_p2

            # Update X,Y entry fields to reflect changes
            self._cal_point_entries['Cal 1']['x'].delete(0, tk.END)
            self._cal_point_entries['Cal 1']['x'].insert(0, f"{cal_p1[0]:.1f}")
            self._cal_point_entries['Cal 1']['y'].delete(0, tk.END)
            self._cal_point_entries['Cal 1']['y'].insert(0, f"{cal_p1[1]:.1f}")
            self._cal_point_entries['Cal 2']['x'].delete(0, tk.END)
            self._cal_point_entries['Cal 2']['x'].insert(0, f"{cal_p2[0]:.1f}")
            self._cal_point_entries['Cal 2']['y'].delete(0, tk.END)
            self._cal_point_entries['Cal 2']['y'].insert(0, f"{cal_p2[1]:.1f}")

            # Update calibration points on the arena plot
            if hasattr(self.arena_maker_window, 'update_calibration_points'):
                self.arena_maker_window.update_calibration_points(cal_p1, cal_p2)

            # Recalculate calibration distances for all receivers
            for receiver_id in range(6):
                pos = self.settings_module.get_receiver_position(receiver_id)
                if pos:
                    rx, ry = pos
                    dist_1 = math.sqrt((rx - cal_p1[0])**2 + (ry - cal_p1[1])**2)
                    dist_2 = math.sqrt((rx - cal_p2[0])**2 + (ry - cal_p2[1])**2)
                    self.settings_module.set_receiver_cal_distances(receiver_id, [dist_1, dist_2])

        except ValueError:
            pass  # Invalid input, don't update

    def _verify_and_save_settings(self):
        """Verify all settings and save to toml file."""
        # First, make sure all displayed values are saved to settings module
        # This is already done by the entry field bindings, but let's be explicit
        try:
            # Update calibration points from entries
            if self._cal_point_entries:
                cal1_x = float(self._cal_point_entries['Cal 1']['x'].get())
                cal1_y = float(self._cal_point_entries['Cal 1']['y'].get())
                cal2_x = float(self._cal_point_entries['Cal 2']['x'].get())
                cal2_y = float(self._cal_point_entries['Cal 2']['y'].get())
                self.settings_module.cal_point_1 = [cal1_x, cal1_y]
                self.settings_module.cal_point_2 = [cal2_x, cal2_y]

            # Update receiver positions from current arena maker window state
            if self.arena_maker_window:
                for receiver_id, (x, y) in self.arena_maker_window.receiver_positions:
                    self.settings_module.set_receiver_position(receiver_id, x, y)

            # Recalculate calibration distances
            cal_p1 = self.settings_module.cal_point_1
            cal_p2 = self.settings_module.cal_point_2
            for receiver_id in range(6):
                pos = self.settings_module.get_receiver_position(receiver_id)
                if pos:
                    rx, ry = pos
                    dist_1 = math.sqrt((rx - cal_p1[0])**2 + (ry - cal_p1[1])**2)
                    dist_2 = math.sqrt((rx - cal_p2[0])**2 + (ry - cal_p2[1])**2)
                    self.settings_module.set_receiver_cal_distances(receiver_id, [dist_1, dist_2])

        except ValueError as e:
            self._show_verify_result(False, [f"Invalid value in entry field: {e}"])
            return

        # Now verify settings
        is_valid, errors = self.settings_module.verify_settings()

        if is_valid:
            # Set valid_settings flag to true (this also saves the config)
            self.settings_module.valid_settings = True

            # Transform the UI to normal operation mode:
            # - Change top button from "Re-dimension Arena" to "Back"
            # - Show the secondary re-dimension button
            if self._arena_maker_top_button:
                self._arena_maker_top_button.config(
                    text="← Back",
                    command=self.show_main_window,
                    bg='#39FF14',  # Neon green
                    activebackground='#2BCC10'
                )
            if self._arena_maker_redim_button:
                self._arena_maker_redim_button.pack(side=tk.RIGHT, padx=5)

            self._show_verify_result(True, ["All settings verified successfully!"])
        else:
            self._show_verify_result(False, errors)

    def _show_verify_result(self, success, messages):
        """Show a dialog with verification results."""
        # Pause animations while dialog is open to prevent focus stealing
        self._pause_process_queue()

        dialog = tk.Toplevel(self.root)
        dialog.title("Verification Result")
        dialog.configure(bg='black')
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.focus_force()  # Ensure dialog has focus

        # Center the dialog
        dialog.geometry("500x300")
        dialog.resizable(False, False)

        # Title
        if success:
            title_text = "Settings Verified!"
            title_color = '#00FF00'
        else:
            title_text = "Verification Failed"
            title_color = '#FF4444'

        title_label = tk.Label(
            dialog,
            text=title_text,
            bg='black',
            fg=title_color,
            font=('Arial', 16, 'bold')
        )
        title_label.pack(pady=10)

        # Messages frame with scrollbar
        msg_frame = tk.Frame(dialog, bg='black')
        msg_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # Text widget for messages
        msg_text = tk.Text(
            msg_frame,
            bg='#222222',
            fg='white',
            font=('Arial', 11),
            wrap=tk.WORD,
            height=10
        )
        msg_text.pack(fill=tk.BOTH, expand=True)

        for msg in messages:
            msg_text.insert(tk.END, f"• {msg}\n")
        msg_text.config(state=tk.DISABLED)

        def close_dialog():
            dialog.destroy()
            self._resume_process_queue()
            self.root.focus_force()

        # OK button
        ok_button = tk.Button(
            dialog,
            text="OK",
            command=close_dialog,
            bg='#00FFFF',
            fg='black',
            font=('Arial', 12, 'bold'),
            activebackground='#00CCCC',
            activeforeground='black',
            relief=tk.RAISED,
            bd=2,
            padx=20,
            pady=5
        )
        ok_button.pack(pady=10)

        # Handle Enter key
        dialog.bind('<Return>', lambda e: close_dialog())

    def _ask_arena_dimensions(self):
        """
        Show a dialog to ask the user for arena width and height.

        Returns:
            tuple (width, height) or (None, None) if cancelled
        """
        # Pause animations while dialog is open to prevent focus stealing
        self._pause_process_queue()

        dialog = tk.Toplevel(self.root)
        dialog.title("Arena Dimensions")
        dialog.configure(bg='black')
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.focus_force()  # Ensure dialog has focus

        # Center the dialog
        dialog.geometry("400x200")
        dialog.resizable(False, False)
        
        result = {'width': None, 'height': None, 'cancelled': False}
        
        # Title
        title_label = tk.Label(
            dialog,
            text="Enter Arena Dimensions",
            bg='black',
            fg='#00FFFF',
            font=('Arial', 16, 'bold')
        )
        title_label.pack(pady=10)
        
        # Width input
        width_frame = tk.Frame(dialog, bg='black')
        width_frame.pack(pady=5)
        tk.Label(
            width_frame,
            text="Width (cm):",
            bg='black',
            fg='white',
            font=('Arial', 12),
            width=15
        ).pack(side=tk.LEFT, padx=5)
        width_entry = tk.Entry(width_frame, font=('Arial', 12), width=15)
        width_entry.pack(side=tk.LEFT, padx=5)
        width_entry.focus()
        
        # Height input
        height_frame = tk.Frame(dialog, bg='black')
        height_frame.pack(pady=5)
        tk.Label(
            height_frame,
            text="Height (cm):",
            bg='black',
            fg='white',
            font=('Arial', 12),
            width=15
        ).pack(side=tk.LEFT, padx=5)
        height_entry = tk.Entry(height_frame, font=('Arial', 12), width=15)
        height_entry.pack(side=tk.LEFT, padx=5)
        
        # Error message frame (initially empty)
        error_frame = tk.Frame(dialog, bg='black')
        error_frame.pack(pady=5)
        error_label = tk.Label(
            error_frame,
            text="",
            bg='black',
            fg='red',
            font=('Arial', 10)
        )
        error_label.pack()
        
        def on_ok():
            # Clear any previous error
            error_label.config(text="")
            
            try:
                width = float(width_entry.get())
                height = float(height_entry.get())
                if width > 0 and height > 0:
                    result['width'] = width
                    result['height'] = height
                    dialog.destroy()
                else:
                    # Show error
                    error_label.config(text="Dimensions must be positive numbers!")
            except ValueError:
                # Show error
                error_label.config(text="Please enter valid numbers!")
            #self.root.force_focus()
        
        def on_cancel():
            result['cancelled'] = True
            dialog.destroy()
        
        # Buttons
        button_frame = tk.Frame(dialog, bg='black')
        button_frame.pack(pady=10)
        
        ok_button = tk.Button(
            button_frame,
            text="OK",
            command=on_ok,
            bg='#39FF14',
            fg='black',
            font=('Arial', 12, 'bold'),
            width=10,
            padx=10,
            pady=5
        )
        ok_button.pack(side=tk.LEFT, padx=10)
        
        cancel_button = tk.Button(
            button_frame,
            text="Cancel",
            command=on_cancel,
            bg='#FF0000',
            fg='white',
            font=('Arial', 12, 'bold'),
            width=10,
            padx=10,
            pady=5
        )
        cancel_button.pack(side=tk.LEFT, padx=10)
        
        # Handle Enter key
        width_entry.bind('<Return>', lambda e: height_entry.focus())
        height_entry.bind('<Return>', lambda e: on_ok())
        
        # Wait for dialog to close
        dialog.wait_window()

        # Resume animations after dialog closes
        self._resume_process_queue()

        # Return focus to main window
        self.root.focus_force()

        if result['cancelled']:
            return None, None
        return result['width'], result['height']

    def _redimension_arena(self):
        """Handle re-dimension button click - ask for new dimensions and redraw arena."""
        width, height = self._ask_arena_dimensions()
        if width is None or height is None:
            # User cancelled, do nothing
            return

        # Calculate receiver positions from dimensions
        new_receiver_positions = self._calculate_receiver_positions_from_dimensions(width, height)

        # Update the arena maker window with new positions
        self.arena_maker_window.update_receiver_positions(new_receiver_positions)

        # Update settings module with new positions
        for receiver_id, (x, y) in new_receiver_positions:
            self.settings_module.set_receiver_position(receiver_id, x, y)

        # Initialize calibration points: X=0, Y=±height/4
        self.settings_module.cal_point_1 = [0.0, height / 4.0]
        self.settings_module.cal_point_2 = [0.0, -height / 4.0]

        # Update calibration points on the arena plot
        if hasattr(self.arena_maker_window, 'update_calibration_points'):
            self.arena_maker_window.update_calibration_points(
                self.settings_module.cal_point_1,
                self.settings_module.cal_point_2
            )

        # Calculate distances from calibration points to each receiver
        cal_p1 = self.settings_module.cal_point_1
        cal_p2 = self.settings_module.cal_point_2
        for receiver_id, (rx, ry) in new_receiver_positions:
            # Distance from cal_point_1 to this receiver
            dist_1 = math.sqrt((rx - cal_p1[0])**2 + (ry - cal_p1[1])**2)
            # Distance from cal_point_2 to this receiver
            dist_2 = math.sqrt((rx - cal_p2[0])**2 + (ry - cal_p2[1])**2)
            # Store in cal_distances array [dist_to_cal1, dist_to_cal2]
            self.settings_module.set_receiver_cal_distances(receiver_id, [dist_1, dist_2])

        # Update distance entry fields with new values
        if self._distance_entries:
            current_distances = self._calculate_distances_from_positions(new_receiver_positions)
            distance_names = ['R1', 'R2', 'R3', 'R4', 'R5', 'R6', 'R7', 'R8', 'R9', 'R10']
            for i, name in enumerate(distance_names):
                if name in self._distance_entries:
                    entry = self._distance_entries[name]
                    entry.delete(0, tk.END)
                    entry.insert(0, f"{current_distances[i]:.1f}")

        # Update calibration point entry fields with new values
        if self._cal_point_entries:
            cal_p1 = self.settings_module.cal_point_1
            cal_p2 = self.settings_module.cal_point_2
            self._cal_point_entries['Cal 1']['x'].delete(0, tk.END)
            self._cal_point_entries['Cal 1']['x'].insert(0, f"{cal_p1[0]:.1f}")
            self._cal_point_entries['Cal 1']['y'].delete(0, tk.END)
            self._cal_point_entries['Cal 1']['y'].insert(0, f"{cal_p1[1]:.1f}")
            self._cal_point_entries['Cal 2']['x'].delete(0, tk.END)
            self._cal_point_entries['Cal 2']['x'].insert(0, f"{cal_p2[0]:.1f}")
            self._cal_point_entries['Cal 2']['y'].delete(0, tk.END)
            self._cal_point_entries['Cal 2']['y'].insert(0, f"{cal_p2[1]:.1f}")
            # Update RC entries
            if 'RC_1' in self._cal_point_entries:
                rc1_dist = math.sqrt(cal_p1[0]**2 + cal_p1[1]**2)
                self._cal_point_entries['RC_1'].delete(0, tk.END)
                self._cal_point_entries['RC_1'].insert(0, f"{rc1_dist:.1f}")
            if 'RC_2' in self._cal_point_entries:
                rc2_dist = math.sqrt(cal_p2[0]**2 + cal_p2[1]**2)
                self._cal_point_entries['RC_2'].delete(0, tk.END)
                self._cal_point_entries['RC_2'].insert(0, f"{rc2_dist:.1f}")

    def show_arena_maker_window(self):
        """
        Display the ArenaMakerWindow in a tkinter frame.
        Frees resources from PositionWindow and shows the arena maker view.
        If settings are not valid, prompts user for arena dimensions and recalculates receiver positions.
        """
        if not self.root:
            return

        # Check if settings are not valid - if so, ask for dimensions and recalculate
        settings_invalid = not self.settings_module.valid_settings
        if settings_invalid:
            width, height = self._ask_arena_dimensions()
            if width is None or height is None:
                # User cancelled, go back to main window (which will redirect back here)
                self._resume_process_queue()
                return
            
            # Calculate receiver positions from dimensions
            new_receiver_positions = self._calculate_receiver_positions_from_dimensions(width, height)

            # Update the arena maker window with new positions
            self.arena_maker_window.update_receiver_positions(new_receiver_positions)
            
            # Update settings module with new positions
            for receiver_id, (x, y) in new_receiver_positions:
                self.settings_module.set_receiver_position(receiver_id, x, y)

            # Initialize calibration points: X=0, Y=±height/4
            self.settings_module.cal_point_1 = [0.0, height / 4.0]
            self.settings_module.cal_point_2 = [0.0, -height / 4.0]

            # Update calibration points on the arena plot
            if hasattr(self.arena_maker_window, 'update_calibration_points'):
                self.arena_maker_window.update_calibration_points(
                    self.settings_module.cal_point_1,
                    self.settings_module.cal_point_2
                )

            # Calculate distances from calibration points to each receiver
            cal_p1 = self.settings_module.cal_point_1
            cal_p2 = self.settings_module.cal_point_2
            for receiver_id, (rx, ry) in new_receiver_positions:
                # Distance from cal_point_1 to this receiver
                dist_1 = math.sqrt((rx - cal_p1[0])**2 + (ry - cal_p1[1])**2)
                # Distance from cal_point_2 to this receiver
                dist_2 = math.sqrt((rx - cal_p2[0])**2 + (ry - cal_p2[1])**2)
                # Store in cal_distances array [dist_to_cal1, dist_to_cal2]
                self.settings_module.set_receiver_cal_distances(receiver_id, [dist_1, dist_2])

        # Pause queue processing during window change
        self._pause_process_queue()

        # Stop the position update thread
        if self.position_window:
            self.position_window.update_thread_run = False

        # Remove PositionWindow canvas from active_animations
        if self.position_window and hasattr(self.position_window, 'canvas'):
            if self.position_window.canvas in self.active_animations:
                self.active_animations.remove(self.position_window.canvas)

        # Hide all frames instead of destroying
        self._hide_all_frames()

        # Update arena maker window with current settings from settings file (when settings are valid)
        if not settings_invalid:
            receiver_positions = self.settings_module.get_tower_coordinates()
            self.arena_maker_window.update_arena(receiver_positions)

        # If arena maker frame already exists, update button visibility and show it
        if self._arena_maker_frame:
            # Update button based on settings validity
            if self._arena_maker_top_button:
                if settings_invalid:
                    # Show re-dimension button at top, hide secondary redim button
                    self._arena_maker_top_button.config(text="Re-dimension Arena", command=self._redimension_arena)
                    if self._arena_maker_redim_button:
                        self._arena_maker_redim_button.pack_forget()
                else:
                    # Show back button, show secondary redim button
                    self._arena_maker_top_button.config(text="← Back", command=self.show_main_window)
                    if self._arena_maker_redim_button:
                        self._arena_maker_redim_button.pack(side=tk.RIGHT, padx=5)

            # Update distance entry fields with current values
            if self._distance_entries:
                current_distances = self._calculate_distances_from_positions(
                    self.arena_maker_window.receiver_positions
                )
                distance_names = ['R1', 'R2', 'R3', 'R4', 'R5', 'R6', 'R7', 'R8', 'R9', 'R10']
                for i, name in enumerate(distance_names):
                    if name in self._distance_entries:
                        entry = self._distance_entries[name]
                        entry.delete(0, tk.END)
                        entry.insert(0, f"{current_distances[i]:.1f}")

            # Update calibration points display
            if hasattr(self.arena_maker_window, 'update_calibration_points'):
                self.arena_maker_window.update_calibration_points(
                    self.settings_module.cal_point_1,
                    self.settings_module.cal_point_2
                )

            # Update calibration point entry fields with current values
            if self._cal_point_entries:
                cal_p1 = self.settings_module.cal_point_1
                cal_p2 = self.settings_module.cal_point_2
                self._cal_point_entries['Cal 1']['x'].delete(0, tk.END)
                self._cal_point_entries['Cal 1']['x'].insert(0, f"{cal_p1[0]:.1f}")
                self._cal_point_entries['Cal 1']['y'].delete(0, tk.END)
                self._cal_point_entries['Cal 1']['y'].insert(0, f"{cal_p1[1]:.1f}")
                self._cal_point_entries['Cal 2']['x'].delete(0, tk.END)
                self._cal_point_entries['Cal 2']['x'].insert(0, f"{cal_p2[0]:.1f}")
                self._cal_point_entries['Cal 2']['y'].delete(0, tk.END)
                self._cal_point_entries['Cal 2']['y'].insert(0, f"{cal_p2[1]:.1f}")
                # Update RC entries
                if 'RC_1' in self._cal_point_entries:
                    rc1_dist = math.sqrt(cal_p1[0]**2 + cal_p1[1]**2)
                    self._cal_point_entries['RC_1'].delete(0, tk.END)
                    self._cal_point_entries['RC_1'].insert(0, f"{rc1_dist:.1f}")
                if 'RC_2' in self._cal_point_entries:
                    rc2_dist = math.sqrt(cal_p2[0]**2 + cal_p2[1]**2)
                    self._cal_point_entries['RC_2'].delete(0, tk.END)
                    self._cal_point_entries['RC_2'].insert(0, f"{rc2_dist:.1f}")

            self._arena_maker_frame.pack(fill=tk.BOTH, expand=True)
            # Re-add canvas to active_animations
            if self.arena_maker_window.canvas not in self.active_animations:
                self.active_animations.append(self.arena_maker_window.canvas)
            self._resume_process_queue()
            return

        # First time setup - create the frame
        self._arena_maker_frame = tk.Frame(self.root, bg='black')
        self._arena_maker_frame.pack(fill=tk.BOTH, expand=True)

        # Create a top bar frame for the button
        top_bar = tk.Frame(self._arena_maker_frame, bg='black')
        top_bar.pack(fill=tk.X, pady=5)

        # Create button based on settings validity
        if settings_invalid:
            # Show re-dimension button when settings are invalid
            top_button = tk.Button(
                top_bar,
                text="Re-dimension Arena",
                command=self._redimension_arena,
                bg='#00FFFF',  # Neon cyan
                fg='black',
                font=('Arial', 12, 'bold'),
                activebackground='#00CCCC',
                activeforeground='black',
                relief=tk.RAISED,
                bd=2,
                padx=10,
                pady=5
            )
        else:
            # Show back button when settings are valid
            top_button = tk.Button(
                top_bar,
                text="← Back",
                command=self.show_main_window,
                bg='#39FF14',  # Neon green
                fg='black',
                font=('Arial', 12, 'bold'),
                activebackground='#2BCC10',
                activeforeground='black',
                relief=tk.RAISED,
                bd=2,
                padx=10,
                pady=5
            )
        
        top_button.pack(side=tk.RIGHT, padx=10)
        self._arena_maker_top_button = top_button

        # Create re-dimension button (shown below Back button during normal operation)
        redim_button = tk.Button(
            top_bar,
            text="Re-dimension",
            command=self._redimension_arena,
            bg='#00FFFF',  # Neon cyan
            fg='black',
            font=('Arial', 10, 'bold'),
            activebackground='#00CCCC',
            activeforeground='black',
            relief=tk.RAISED,
            bd=2,
            padx=8,
            pady=3
        )
        self._arena_maker_redim_button = redim_button
        # Only show redim button during normal operation (valid settings)
        if not settings_invalid:
            redim_button.pack(side=tk.RIGHT, padx=5)

        # Title label
        title_label = tk.Label(
            top_bar,
            text="Arena Maker",
            bg='black',
            fg='#00FFFF',  # Neon cyan
            font=('Arial', 16, 'bold')
        )
        title_label.pack(side=tk.LEFT, padx=10)

        # Restore Focus button - workaround for when keyboard focus is lost
        restore_focus_button = tk.Button(
            top_bar,
            text="Restore Keyboard",
            command=lambda: self.root.focus_set(),
            bg='#444444',
            fg='white',
            font=('Arial', 9),
            activebackground='#666666',
            activeforeground='white',
            relief=tk.RAISED,
            bd=1,
            padx=5,
            pady=2
        )
        restore_focus_button.pack(side=tk.LEFT, padx=10)

        # Embed the matplotlib figure in tkinter
        canvas = FigureCanvasTkAgg(self.arena_maker_window.fig, master=self._arena_maker_frame)
        self.arena_maker_window.canvas = canvas
        canvas.draw_idle()  # Non-blocking draw
        canvas_widget = canvas.get_tk_widget()
        canvas_widget.configure(takefocus=False)  # Prevent canvas from stealing keyboard focus
        canvas_widget.pack(fill=tk.BOTH, expand=True)

        # Store reference to graphics module for thread-safe updates
        self.arena_maker_window._graphics_module = self

        # Create distance control panel below the canvas
        distance_frame = tk.Frame(self._arena_maker_frame, bg='black')
        distance_frame.pack(fill=tk.X, pady=10)

        # Calculate current distances from receiver positions
        current_distances = self._calculate_distances_from_positions(
            self.arena_maker_window.receiver_positions
        )

        # Distance labels and descriptions
        distance_info = [
            ('R1', 'Rcvr 3→2', current_distances[0]),
            ('R2', 'Rcvr 2→1', current_distances[1]),
            ('R3', 'Rcvr 1→4', current_distances[2]),
            ('R4', 'Rcvr 4→5', current_distances[3]),
            ('R5', 'Rcvr 5→6', current_distances[4]),
            ('R6', 'Rcvr 6→3', current_distances[5]),
            ('R7', 'Origin→2 X', current_distances[6]),
            ('R8', 'Origin→5 X', current_distances[7]),
            ('R9', 'Rcvr2 Y-ofs', current_distances[8]),
            ('R10', 'Rcvr5 Y-ofs', current_distances[9])
        ]

        # Create entry fields for each distance
        self._distance_entries = {}

        for name, desc, value in distance_info:
            entry_frame = tk.Frame(distance_frame, bg='black')
            entry_frame.pack(side=tk.LEFT, padx=10, expand=True)

            # Label with distance name and description
            label = tk.Label(
                entry_frame,
                text=f"{name}\n({desc})",
                bg='black',
                fg='#00FFFF',
                font=('Arial', 10, 'bold')
            )
            label.pack()

            # Entry field
            entry = tk.Entry(
                entry_frame,
                font=('Arial', 12),
                width=10,
                justify='center'
            )
            entry.insert(0, f"{value:.1f}")
            entry.pack(pady=2)

            # Store reference
            self._distance_entries[name] = entry

            # Bind Enter key and focus-out to update
            entry.bind('<Return>', lambda e: self._update_arena_from_distances())
            entry.bind('<FocusOut>', lambda e: self._update_arena_from_distances())

        # Create calibration point control panel below the distance panel
        cal_point_frame = tk.Frame(self._arena_maker_frame, bg='black')
        cal_point_frame.pack(fill=tk.X, pady=10)

        # Title for calibration points section
        cal_title = tk.Label(
            cal_point_frame,
            text="Calibration Points",
            bg='black',
            fg='#00FF00',
            font=('Arial', 12, 'bold')
        )
        cal_title.pack(pady=5)

        # Frame for calibration point entries
        cal_entries_frame = tk.Frame(cal_point_frame, bg='black')
        cal_entries_frame.pack()

        # Get current calibration point values
        cal_p1 = self.settings_module.cal_point_1
        cal_p2 = self.settings_module.cal_point_2

        # Calibration point entry info: (name, description, x_value, y_value)
        cal_point_info = [
            ('Cal 1', 'X', 'Y', cal_p1[0], cal_p1[1]),
            ('Cal 2', 'X', 'Y', cal_p2[0], cal_p2[1])
        ]

        # Store calibration point entry references
        self._cal_point_entries = {}

        for name, x_label, y_label, x_val, y_val in cal_point_info:
            point_frame = tk.Frame(cal_entries_frame, bg='black')
            point_frame.pack(side=tk.LEFT, padx=20)

            # Label for calibration point name
            label = tk.Label(
                point_frame,
                text=name,
                bg='black',
                fg='#00FF00',
                font=('Arial', 10, 'bold')
            )
            label.pack()

            # X coordinate entry
            x_frame = tk.Frame(point_frame, bg='black')
            x_frame.pack()
            tk.Label(
                x_frame,
                text="X:",
                bg='black',
                fg='#00FF00',
                font=('Arial', 10)
            ).pack(side=tk.LEFT)
            x_entry = tk.Entry(
                x_frame,
                font=('Arial', 12),
                width=8,
                justify='center'
            )
            x_entry.insert(0, f"{x_val:.1f}")
            x_entry.pack(side=tk.LEFT, padx=2)

            # Y coordinate entry
            y_frame = tk.Frame(point_frame, bg='black')
            y_frame.pack()
            tk.Label(
                y_frame,
                text="Y:",
                bg='black',
                fg='#00FF00',
                font=('Arial', 10)
            ).pack(side=tk.LEFT)
            y_entry = tk.Entry(
                y_frame,
                font=('Arial', 12),
                width=8,
                justify='center'
            )
            y_entry.insert(0, f"{y_val:.1f}")
            y_entry.pack(side=tk.LEFT, padx=2)

            # Store references
            self._cal_point_entries[name] = {'x': x_entry, 'y': y_entry}

            # Bind Enter key and focus-out to update
            x_entry.bind('<Return>', lambda e: self._update_calibration_points_from_entries())
            x_entry.bind('<FocusOut>', lambda e: self._update_calibration_points_from_entries())
            y_entry.bind('<Return>', lambda e: self._update_calibration_points_from_entries())
            y_entry.bind('<FocusOut>', lambda e: self._update_calibration_points_from_entries())

        # Add RC_1 and RC_2 controls (distance from origin to calibration points)
        rc_frame = tk.Frame(cal_entries_frame, bg='black')
        rc_frame.pack(side=tk.LEFT, padx=20)

        # RC_1 entry
        rc1_frame = tk.Frame(rc_frame, bg='black')
        rc1_frame.pack()
        tk.Label(
            rc1_frame,
            text="RC_1:",
            bg='black',
            fg='#00FF00',
            font=('Arial', 10, 'bold')
        ).pack(side=tk.LEFT)
        rc1_entry = tk.Entry(
            rc1_frame,
            font=('Arial', 12),
            width=8,
            justify='center'
        )
        rc1_dist = math.sqrt(cal_p1[0]**2 + cal_p1[1]**2)
        rc1_entry.insert(0, f"{rc1_dist:.1f}")
        rc1_entry.pack(side=tk.LEFT, padx=2)

        # RC_2 entry
        rc2_frame = tk.Frame(rc_frame, bg='black')
        rc2_frame.pack()
        tk.Label(
            rc2_frame,
            text="RC_2:",
            bg='black',
            fg='#00FF00',
            font=('Arial', 10, 'bold')
        ).pack(side=tk.LEFT)
        rc2_entry = tk.Entry(
            rc2_frame,
            font=('Arial', 12),
            width=8,
            justify='center'
        )
        rc2_dist = math.sqrt(cal_p2[0]**2 + cal_p2[1]**2)
        rc2_entry.insert(0, f"{rc2_dist:.1f}")
        rc2_entry.pack(side=tk.LEFT, padx=2)

        # Store RC entry references
        self._cal_point_entries['RC_1'] = rc1_entry
        self._cal_point_entries['RC_2'] = rc2_entry

        # Bind RC entries
        rc1_entry.bind('<Return>', lambda e: self._update_rc_from_entries())
        rc1_entry.bind('<FocusOut>', lambda e: self._update_rc_from_entries())
        rc2_entry.bind('<Return>', lambda e: self._update_rc_from_entries())
        rc2_entry.bind('<FocusOut>', lambda e: self._update_rc_from_entries())

        # Add Verify Settings button
        verify_frame = tk.Frame(cal_entries_frame, bg='black')
        verify_frame.pack(side=tk.LEFT, padx=20)
        verify_button = tk.Button(
            verify_frame,
            text="Verify Settings",
            command=self._verify_and_save_settings,
            bg='#FFD700',  # Gold
            fg='black',
            font=('Arial', 12, 'bold'),
            activebackground='#FFC000',
            activeforeground='black',
            relief=tk.RAISED,
            bd=2,
            padx=10,
            pady=5
        )
        verify_button.pack()

        # Update calibration points display on the plot
        if hasattr(self.arena_maker_window, 'update_calibration_points'):
            self.arena_maker_window.update_calibration_points(
                self.settings_module.cal_point_1,
                self.settings_module.cal_point_2
            )

        # Add canvas to active_animations for automatic refreshing
        if hasattr(self.arena_maker_window, 'canvas') and self.arena_maker_window.canvas in self.active_animations:
            self.active_animations.remove(self.arena_maker_window.canvas)
        self.active_animations.append(canvas)

        # Resume queue processing after window change is complete
        self._resume_process_queue()

    def show_main_window(self):
        """
        Display the MainWindow with navigation buttons.
        Shows title "Ultra GPS Control" and three buttons: Position, Calibration, Setup.
        If settings are not valid, shows the Arena Maker window instead.
        """
        if not self.root:
            return

        # Check if settings are valid before showing main window
        if not self.settings_module.valid_settings:
            # Settings are not valid, show arena maker window instead
            self.show_arena_maker_window()
            return

        # Pause queue processing during window change
        self._pause_process_queue()

        # Stop the position update thread
        if self.position_window:
            self.position_window.update_thread_run = False

        # Remove all canvases from active_animations
        if self.position_window and hasattr(self.position_window, 'canvas'):
            if self.position_window.canvas in self.active_animations:
                self.active_animations.remove(self.position_window.canvas)
        if self.calibration_window and hasattr(self.calibration_window, 'canvas'):
            if self.calibration_window.canvas in self.active_animations:
                self.active_animations.remove(self.calibration_window.canvas)
        if self.arena_maker_window and hasattr(self.arena_maker_window, 'canvas'):
            if self.arena_maker_window.canvas in self.active_animations:
                self.active_animations.remove(self.arena_maker_window.canvas)

        # Hide all frames instead of destroying
        self._hide_all_frames()

        # If main frame already exists, just show it
        if self._main_frame:
            self._main_frame.pack(fill=tk.BOTH, expand=True)
            # Ensure root window is visible and updated
            self.root.deiconify()
            self.root.update()
            self._resume_process_queue()
            return

        # First time setup - create the frame
        self._main_frame = tk.Frame(self.root, bg='black')
        self._main_frame.pack(fill=tk.BOTH, expand=True)

        # Set up button callbacks before building
        self.main_window.on_position_click = self.show_position_window
        self.main_window.on_calibration_click = self.show_calibration_window
        self.main_window.on_setup_click = self.show_arena_maker_window

        # Build the MainWindow UI inside the frame
        self.main_window.build(self._main_frame)

        # Ensure root window is visible and updated
        self.root.deiconify()
        self.root.update()

        # Resume queue processing after window change is complete
        self._resume_process_queue()

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
    def __init__(self, receiver_positions=None):
        """
        Initialize the PositionWindow with matplotlib.

        Args:
            receiver_positions (list of tuples): List of (id, (x, y)) positions for 6 receivers.
                "id" is a 0-indexed id that denotes the tower coordinate to the tower in the real world
                The label created is index+1 to mimic real-world labels which are 1-indexed
        """
        self.grid_padding = 20 # Extra padding on the side to make receivers visible
        self.position_history = deque(maxlen=50)  # Store last 50 positions

        # Store compass rose elements for updating
        self.compass_x_arrow = None
        self.compass_x_text = None
        self.compass_y_arrow = None
        self.compass_y_text = None

        # Set receiver positions (6 receivers in a rectangular arrangement if not provided)
        if receiver_positions is None:
            raise ValueError("Must provide receiver positions!")
        else:
            if len(receiver_positions) != 6:
                raise ValueError("Must provide exactly 6 receiver positions")
            self.receiver_positions = receiver_positions

        # Calculate axis limits from receiver positions
        receiver_x = [pos[1][0] for pos in receiver_positions]
        receiver_y = [pos[1][1] for pos in receiver_positions]
        min_x, max_x = min(receiver_x), max(receiver_x)
        min_y, max_y = min(receiver_y), max(receiver_y)

        # Initialize matplotlib figure with GridSpec layout
        # Layout: [distance_text_left, left_plots, main_arena, right_plots, distance_text_right]
        from matplotlib.gridspec import GridSpec

        self.fig = plt.figure(figsize=(16, 10), facecolor='black')
        # 3 rows x 3 columns for main content (position text moved to tkinter)
        gs = GridSpec(3, 3, figure=self.fig, width_ratios=[1, 5, 1], height_ratios=[1, 1, 1], hspace=0.3, wspace=0.4)

        # Create main arena axes in center (spans rows 0-2)
        self.ax = self.fig.add_subplot(gs[0:3, 1])
        self.ax.set_facecolor('black')
        self.ax.set_xlim(min_x - self.grid_padding, max_x + self.grid_padding)
        self.ax.set_ylim(min_y - self.grid_padding, max_y + self.grid_padding)
        self.ax.set_aspect('equal')
        self.ax.grid(True, alpha=0.3, color='gray')
        self.ax.set_xlabel('X Position', color='white')
        self.ax.set_ylabel('Y Position', color='white')
        self.ax.set_title('Vehicle Position Tracking', color='white')
        self.ax.tick_params(colors='white')
        for spine in self.ax.spines.values():
            spine.set_color('white')

        # Create distance plot axes: left side (1-3), right side (4-6)
        self.distance_histories = [deque(maxlen=50) for _ in range(6)]
        self.distance_insets = []
        self.distance_lines = []

        # Distance windows: text labels showing current distance (stored for updating)
        self.distance_windows = []

        # LED indicators for receiver sanity state (green = sane, red = not sane)
        self.sane_leds = []

        # Left column: receivers 1, 2, 3 (indices 0, 1, 2) - column 0
        for row in range(3):
            ax_dist = self.fig.add_subplot(gs[row, 0])
            ax_dist.set_facecolor('black')
            ax_dist.set_title(f'Receiver {row + 1}', fontsize=10, color='white')
            ax_dist.tick_params(axis='both', labelsize=8, colors='white')
            ax_dist.set_xlim(0, 50)
            ax_dist.set_ylim(0, 500)
            ax_dist.set_xlabel('Sample', fontsize=8, color='white')
            ax_dist.set_ylabel('Distance (cm)', fontsize=8, color='white')
            ax_dist.yaxis.set_label_position('right')
            ax_dist.yaxis.tick_right()
            ax_dist.grid(True, alpha=0.3, color='gray')
            for spine in ax_dist.spines.values():
                spine.set_color('white')
            line, = ax_dist.plot([], [], color='#39FF14', linewidth=1)  # Neon green
            # Add distance_window text at top right of left plots
            distance_window = ax_dist.text(0.98, 0.95, '---',
                                           ha='right', va='top', fontsize=12,
                                           fontweight='bold', transform=ax_dist.transAxes,
                                           color='white',
                                           bbox=dict(boxstyle='round', facecolor='#222222', alpha=0.8))
            # Add LED indicator at top left of left plots (green = sane, red = not sane)
            from matplotlib.patches import Circle
            led = Circle((0.08, 0.88), 0.06, transform=ax_dist.transAxes,
                        facecolor='#39FF14', edgecolor='white', linewidth=1.5, zorder=10)
            ax_dist.add_patch(led)
            self.distance_insets.append(ax_dist)
            self.distance_lines.append(line)
            self.distance_windows.append(distance_window)
            self.sane_leds.append(led)

        # Right column: receivers 4, 5, 6 (indices 3, 4, 5) - column 2
        for row in range(3):
            ax_dist = self.fig.add_subplot(gs[row, 2])
            ax_dist.set_facecolor('black')
            ax_dist.set_title(f'Receiver {row + 4}', fontsize=10, color='white')
            ax_dist.tick_params(axis='both', labelsize=8, colors='white')
            ax_dist.set_xlim(0, 50)
            ax_dist.set_ylim(0, 500)
            ax_dist.set_xlabel('Sample', fontsize=8, color='white')
            ax_dist.set_ylabel('Distance (cm)', fontsize=8, color='white')
            ax_dist.yaxis.set_label_position('left')
            ax_dist.yaxis.tick_left()
            ax_dist.grid(True, alpha=0.3, color='gray')
            for spine in ax_dist.spines.values():
                spine.set_color('white')
            line, = ax_dist.plot([], [], color='#39FF14', linewidth=1)  # Neon green
            # Add distance_window text at top left of right plots
            distance_window = ax_dist.text(0.02, 0.95, '---',
                                           ha='left', va='top', fontsize=12,
                                           fontweight='bold', transform=ax_dist.transAxes,
                                           color='white',
                                           bbox=dict(boxstyle='round', facecolor='#222222', alpha=0.8))
            # Add LED indicator at top right of right plots (green = sane, red = not sane)
            from matplotlib.patches import Circle
            led = Circle((0.92, 0.88), 0.06, transform=ax_dist.transAxes,
                        facecolor='#39FF14', edgecolor='white', linewidth=1.5, zorder=10)
            ax_dist.add_patch(led)
            self.distance_insets.append(ax_dist)
            self.distance_lines.append(line)
            self.distance_windows.append(distance_window)
            self.sane_leds.append(led)

        # Initialize compass rose
        self._draw_compass_rose()

        # Draw receivers (neon magenta dots)
        receiver_x = [pos[1][0] for pos in self.receiver_positions]
        receiver_y = [pos[1][1] for pos in self.receiver_positions]
        self.ax.scatter(receiver_x, receiver_y, c='#FF00FF', s=100, zorder=5, label='Receivers')  # Neon magenta

        # Add ID labels next to each receiver+1 (to match real-life labeling)
        for receiver_id, (x, y) in self.receiver_positions:
            self.ax.text(x + 5, y + 5, str(receiver_id+1), color='white',
                        fontsize=10, fontweight='bold', zorder=6,
                        ha='left', va='bottom')

        # Draw neon cyan line connecting receivers (connect in order, then close the loop)
        # Connect receivers in a rectangular pattern
        connection_order = [0, 1, 2, 5, 4, 3, 0]  # Connect around the rectangle
        connected_x = [receiver_x[i] for i in connection_order]
        connected_y = [receiver_y[i] for i in connection_order]
        self.ax.plot(connected_x, connected_y, color='#00FFFF', linewidth=2, alpha=0.7, label='Receiver Connections')  # Neon cyan

        # Initialize vehicle position plot (multilateration_method_1 - neon green)
        self.vehicle_point, = self.ax.plot([], [], 'o', color='#39FF14', markersize=10, zorder=6, label='Multilateration Position')  # Neon green
        self.vehicle_trail, = self.ax.plot([], [], '-', color='#39FF14', linewidth=1, alpha=0.5, label='Multilateration Trail')

        # Initialize CEP position plot (neon yellow)
        self.cep_point, = self.ax.plot([], [], 'o', color='#FFFF00', markersize=10, zorder=6, label='CEP Position')  # Neon yellow
        self.cep_trail, = self.ax.plot([], [], '-', color='#FFFF00', linewidth=1, alpha=0.5, label='CEP Trail')
        self.cep_position_history = deque(maxlen=50)  # Store last 50 CEP positions

        # Median filter buffers for each receiver (window size = 5 samples)
        self.median_filter_window = 5
        self.distance_filter_buffers = [deque(maxlen=self.median_filter_window) for _ in range(6)]

        self.ax.legend(loc='upper right', facecolor='#222222', edgecolor='white', labelcolor='white')

        #plt.tight_layout()

        # Thread running variable
        self.update_thread_run = False

        # EXPERIMENTAL
        from PositionModule import StablePositionEstimator
        from PositionModule import CEPPositioning
        #self.stable_pos = StablePositionEstimator(self.receiver_positions)
        #receiver_coordinates = [cords for index,cords in receiver_positions]
        self.stable_pos = CEPPositioning(receiver_positions, min_transmitters=3)

    def update_arena(self, receiver_positions):
        """
        Update the arena dimensions and receiver positions.
        Axis limits are calculated from actual receiver positions.

        Args:
            receiver_positions (list of tuples): List of (id, (x, y)) positions for 6 receivers
        """
        self.receiver_positions = receiver_positions

        # Calculate bounds from receiver positions
        receiver_x = [pos[1][0] for pos in receiver_positions]
        receiver_y = [pos[1][1] for pos in receiver_positions]
        min_x, max_x = min(receiver_x), max(receiver_x)
        min_y, max_y = min(receiver_y), max(receiver_y)

        # Update main arena axis limits based on actual receiver positions with padding
        self.ax.set_xlim(min_x - self.grid_padding, max_x + self.grid_padding)
        self.ax.set_ylim(min_y - self.grid_padding, max_y + self.grid_padding)

        # Redraw compass rose with new axis limits
        self._draw_compass_rose()

        # Clear existing receiver scatter and labels from main axes
        # We need to redraw receivers since scatter doesn't have set_offsets for easy update
        # Remove old receiver elements (keep other elements like vehicle points)
        for artist in self.ax.collections[:]:
            # Only remove receiver scatter (first collection added)
            if artist.get_label() == 'Receivers':
                artist.remove()

        # Remove old receiver labels (text elements with receiver IDs)
        for text in self.ax.texts[:]:
            txt = text.get_text()
            if txt.isdigit() and 1 <= int(txt) <= 6:
                text.remove()

        # Remove old connection lines
        for line in self.ax.lines[:]:
            if line.get_label() == 'Receiver Connections':
                line.remove()

        # Redraw receivers
        self.ax.scatter(receiver_x, receiver_y, c='#FF00FF', s=100, zorder=5, label='Receivers')

        # Redraw receiver labels
        for receiver_id, (x, y) in receiver_positions:
            self.ax.text(x + 5, y + 5, str(receiver_id + 1), color='white',
                        fontsize=10, fontweight='bold', zorder=6,
                        ha='left', va='bottom')

        # Redraw connection lines
        connection_order = [0, 1, 2, 5, 4, 3, 0]
        connected_x = [receiver_x[i] for i in connection_order]
        connected_y = [receiver_y[i] for i in connection_order]
        self.ax.plot(connected_x, connected_y, color='#00FFFF', linewidth=2, alpha=0.7, label='Receiver Connections')

        # Update CEP positioning with new receiver positions
        from PositionModule import CEPPositioning
        self.stable_pos = CEPPositioning(receiver_positions, min_transmitters=3)

        # Clear position history for fresh start
        self.position_history.clear()
        self.cep_position_history.clear()

        # Redraw canvas if available
        if hasattr(self, 'canvas') and self.canvas:
            self.canvas.draw()

    def _draw_compass_rose(self):
        """Draw or update the X/Y compass rose in the center of the plot."""
        # Remove existing compass rose elements if they exist
        if self.compass_x_arrow:
            self.compass_x_arrow.remove()
        if self.compass_x_text:
            self.compass_x_text.remove()
        if self.compass_y_arrow:
            self.compass_y_arrow.remove()
        if self.compass_y_text:
            self.compass_y_text.remove()

        # Calculate arrow length based on current axis limits
        x_min, x_max = self.ax.get_xlim()
        y_min, y_max = self.ax.get_ylim()
        compass_x = 0
        compass_y = 0
        arrow_length = min((x_max - x_min), (y_max - y_min)) * 0.08  # 8% of smaller dimension

        # Draw X axis arrow (pointing right)
        self.compass_x_arrow = self.ax.annotate('', xy=(compass_x + arrow_length, compass_y),
                        xytext=(compass_x, compass_y),
                        arrowprops=dict(arrowstyle='->', color='white', lw=2, zorder=7))
        self.compass_x_text = self.ax.text(compass_x + arrow_length * 0.5, compass_y - arrow_length * 0.3,
                    'X', color='white', fontsize=12, fontweight='bold',
                    ha='center', va='top', zorder=7)

        # Draw Y axis arrow (pointing up)
        self.compass_y_arrow = self.ax.annotate('', xy=(compass_x, compass_y + arrow_length),
                        xytext=(compass_x, compass_y),
                        arrowprops=dict(arrowstyle='->', color='white', lw=2, zorder=7))
        self.compass_y_text = self.ax.text(compass_x - arrow_length * 0.3, compass_y + arrow_length * 0.5,
                    'Y', color='white', fontsize=12, fontweight='bold',
                    ha='right', va='center', zorder=7)

    def update_cords(self, x, y):
        """
        Update the vehicle coordinates of the plot

        Args:
            x (float): X coordinate of the vehicle
            y (float): Y coordinate of the vehicle
        """
        # Add new position to history
        self.position_history.append((x, y))

        # Update tkinter label if available (schedule on main thread)
        if hasattr(self, 'multilat_label') and hasattr(self, '_graphics_module'):
            text = f'Multilat: ({x:.1f}, {y:.1f})'
            self._graphics_module._schedule_update(
                lambda t=text: self.multilat_label.config(text=t)
            )

        # Check if plotting is enabled
        show_plot = getattr(self, 'show_multilateration', True)

        # Only update plot if multilateration display is enabled
        if show_plot:
            # Update current position (green dot)
            self.vehicle_point.set_data([x], [y])

            # Update position trail (green line showing last 50 positions)
            if len(self.position_history) > 1:
                trail_x = [pos[0] for pos in self.position_history]
                trail_y = [pos[1] for pos in self.position_history]
                self.vehicle_trail.set_data(trail_x, trail_y)
        else:
            # Hide the multilateration position and trail
            self.vehicle_point.set_data([], [])
            self.vehicle_trail.set_data([], [])

        # Note: Canvas will be automatically redrawn by _refresh_animations()
        # which runs every 10ms and calls canvas.draw() on all active_animations

    def update_cep_cords(self, x, y):
        """
        Update the CEP position marker on the plot

        Args:
            x (float): X coordinate from CEP calculation
            y (float): Y coordinate from CEP calculation
        """
        # Add new position to CEP history
        self.cep_position_history.append((x, y))

        # Update tkinter label if available (schedule on main thread)
        if hasattr(self, 'cep_label') and hasattr(self, '_graphics_module'):
            text = f'CEP: ({x:.1f}, {y:.1f})'
            self._graphics_module._schedule_update(
                lambda t=text: self.cep_label.config(text=t)
            )

        # Check if plotting is enabled
        show_plot = getattr(self, 'show_cep', True)

        # Only update plot if CEP display is enabled
        if show_plot:
            # Update current CEP position (yellow dot)
            self.cep_point.set_data([x], [y])

            # Update CEP position trail (yellow line showing last 50 positions)
            if len(self.cep_position_history) > 1:
                trail_x = [pos[0] for pos in self.cep_position_history]
                trail_y = [pos[1] for pos in self.cep_position_history]
                self.cep_trail.set_data(trail_x, trail_y)
        else:
            # Hide the CEP position and trail
            self.cep_point.set_data([], [])
            self.cep_trail.set_data([], [])

        # Note: Canvas will be automatically redrawn by _refresh_animations()

    def update_distances(self, receiver_distances):
        """
        Update the distance graphs for all 6 receivers

        Args:
            receiver_distances: Array of 6 distance values from each receiver
        """
        for i in range(6):
            # Add new distance to history
            distance = receiver_distances[i]
            self.distance_histories[i].append(distance)

            # Update line data
            history = list(self.distance_histories[i])
            x_data = list(range(len(history)))
            self.distance_lines[i].set_data(x_data, history)

            # Update y-axis limits: current distance +/- (distance * 3)
            if len(history) > 0:
                current_dist = history[-1]
                y_center = current_dist
                y_range = current_dist * 3
                y_min = max(0, y_center - y_range)
                y_max = y_center + y_range
                self.distance_insets[i].set_ylim(y_min, y_max)
                self.distance_insets[i].set_xlim(0, max(50, len(history)))

            # Update distance_window text inside each plot
            self.distance_windows[i].set_text(f'{distance:.1f} cm')

        # Note: Canvas will be automatically redrawn by _refresh_animations()

    def update_sane_leds(self, multilat_sane_indices, cep_sane_indices):
        """
        Update the LED indicators for each receiver based on sanity check results.
        A receiver is shown as green if it's sane in BOTH multilateration and CEP,
        red otherwise.

        Args:
            multilat_sane_indices: Array of indices considered sane by PositionModule
            cep_sane_indices: Array of indices considered sane by CEPPositioning
        """
        for i in range(6):
            # Receiver is sane if it appears in both sane indices lists
            is_sane = (i in multilat_sane_indices) and (i in cep_sane_indices)

            if is_sane:
                self.sane_leds[i].set_facecolor('#39FF14')  # Neon green
            else:
                self.sane_leds[i].set_facecolor('#FF0000')  # Red

        # Note: Canvas will be automatically redrawn by _refresh_animations()

    def update_cep_validity_led(self, invalid_position):
        """
        Update the CEP validity LED indicator based on position validation result.
        Green = valid position (within receiver bounds), Red = invalid position.

        Args:
            invalid_position: Boolean, True if position is outside receiver bounds
        """
        if hasattr(self, 'cep_led_canvas') and hasattr(self, 'cep_validity_led'):
            if hasattr(self, '_graphics_module'):
                # Schedule update on main thread for thread safety
                color = '#FF0000' if invalid_position else '#39FF14'
                self._graphics_module._schedule_update(
                    lambda c=color: self.cep_led_canvas.itemconfig(self.cep_validity_led, fill=c)
                )

    def update_insufficient_receivers_led(self, insufficient):
        """
        Update the insufficient receivers LED indicator.
        Green = sufficient receivers (>= 3 sane), Red = insufficient receivers (< 3 sane).

        Args:
            insufficient: Boolean, True if fewer than 3 receivers are sane
        """
        if hasattr(self, 'insuff_led_canvas') and hasattr(self, 'insuff_receivers_led'):
            if hasattr(self, '_graphics_module'):
                # Schedule update on main thread for thread safety
                color = '#FF0000' if insufficient else '#39FF14'
                self._graphics_module._schedule_update(
                    lambda c=color: self.insuff_led_canvas.itemconfig(self.insuff_receivers_led, fill=c)
                )

    def apply_median_filter(self, receiver_distances):
        """
        Apply median filter to raw receiver distances to reduce noise and outliers.

        Args:
            receiver_distances: Array of 6 raw distance values from each receiver

        Returns:
            Array of 6 median-filtered distance values
        """
        filtered_distances = np.zeros(6)

        for i in range(6):
            # Add new distance to filter buffer
            self.distance_filter_buffers[i].append(receiver_distances[i])

            # Calculate median of buffer
            buffer = list(self.distance_filter_buffers[i])
            filtered_distances[i] = np.median(buffer)

        return filtered_distances

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

        # Load receiver offsets from settings module if available
        receiver_offsets = {}
        if graphics_module and hasattr(graphics_module, 'settings_module'):
            settings = graphics_module.settings_module
            for recv_id in range(6):
                offset = settings.get_receiver_offset(recv_id)
                if offset:
                    receiver_offsets[recv_id] = offset

        # Instantiate the position module with the array of all receiver positions and offsets
        pos_module = PositionModule(self.receiver_positions, receiver_offsets=receiver_offsets)

        while self.update_thread_run and (graphics_module is None or graphics_module.running):
            try:
                time_start = time.time()
                # Update position with random values (replace with actual position data)
                #x, y = randint(1, 5), randint(1, 5)

                # Request the system to send a pulse and calculate the distances
                control_module.update()
                # Old method: get pre-calculated distances from server
                # raw_distances = control_module.get_receiver_distances()
                # New method: use serial messages and convert using calibrated offsets
                serial_messages = control_module.get_serial_message()
                raw_distances = pos_module.serial_to_distances(serial_messages)
                print(f"Serial Message: {serial_messages}")
                print(f"Raw Distances (from serial): {raw_distances}")

                # Apply median filter to reduce noise and outliers
                #filtered_distances = self.apply_median_filter(raw_distances)
                #print(f"Filtered Distances: {filtered_distances}")
                filtered_distances = raw_distances

                # Use filtered distances for position calculation
                pos, result = pos_module.multilateration_method_1(filtered_distances, [0,1,2,3,4,5])
                x_calc = pos[0]
                y_calc = pos[1]
                print(f"tick Pos: ({x_calc}, {y_calc})")

                best_pos, best_cep, best_indices, cov, all_results = \
                        self.stable_pos.find_best_subset(filtered_distances, max_subsets_to_try=15)
                print(f"CEP Position: {best_pos}, CEP: {best_cep}, Indices: {best_indices}")

                # Validate that CEP position is within receiver bounds
                validated_pos, invalid_position = self.stable_pos.validate_position(best_pos)
                if invalid_position:
                    print(f"CEP position invalid (outside bounds), using last good position")
                best_pos = validated_pos

                #weighted_cep, final_cep, recv_weights, best_indices = \
                    #self.stable_pos.adaptive_weighted_solution(filtered_distances)

                # OVERRIDE CEP CORDS TO TEST OTHER TACTICS
                #best_pos = weighted_cep

                # Update multilateration position (blue dot)
                self.update_cords(x_calc, y_calc)
                # Update CEP position (orange dot)
                self.update_cep_cords(best_pos[0], best_pos[1])
                # Update distance graphs for all receivers (show raw distances)
                self.update_distances(raw_distances)

                # Update LED indicators based on sane indices from both modules
                multilat_sane = pos_module.last_sane_indices if pos_module.last_sane_indices is not None else []
                cep_sane = self.stable_pos.last_sane_indices if self.stable_pos.last_sane_indices is not None else []
                self.update_sane_leds(multilat_sane, cep_sane)
                print(f"Sane indices - Multilat: {multilat_sane}, CEP: {cep_sane}")

                # Check for insufficient receivers (< 3 sane in either module)
                insufficient_receivers = (len(multilat_sane) < 3) or (len(cep_sane) < 3)
                self.update_insufficient_receivers_led(insufficient_receivers)
                if insufficient_receivers:
                    print(f"WARNING: Insufficient receivers - Multilat: {len(multilat_sane)}, CEP: {len(cep_sane)}")

                # Update CEP validity LED (green = valid, red = invalid position)
                self.update_cep_validity_led(invalid_position)

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

class CalibrationWindow:
    def __init__(self, receiver_positions=None, settings_module=None):
        """
        Initialize the CalibrationWindow with matplotlib.
        Contains position arena, histogram plots for Run 1 and Run 2, and offset display.

        Args:
            receiver_positions (list of tuples): List of (id, (x, y)) positions for 6 receivers.
                "id" is a 0-indexed id that denotes the tower coordinate to the tower in the real world
                The label created is index+1 to mimic real-world labels which are 1-indexed
            settings_module: Reference to SettingsModule for reading/saving offsets
        """
        self.grid_padding = 20  # Extra padding on the side to make receivers visible
        self.position_history = deque(maxlen=50)  # Store last 50 positions
        self.settings_module = settings_module

        # Store compass rose elements for updating
        self.compass_x_arrow = None
        self.compass_x_text = None
        self.compass_y_arrow = None
        self.compass_y_text = None

        # Calibration state
        self.calibration_running = False
        self.current_run = 0  # 0 = not running, 1 = run 1, 2 = run 2

        # Histogram data storage: {receiver_id: {serial_value: count}}
        self.run1_data = {i: {} for i in range(6)}
        self.run2_data = {i: {} for i in range(6)}

        # Calibration point display elements
        self.cal_point_scatter = None
        self.cal_point_label = None

        # Set receiver positions (6 receivers in a rectangular arrangement if not provided)
        if receiver_positions is None:
            raise ValueError("Must provide receiver positions!")
        else:
            if len(receiver_positions) != 6:
                raise ValueError("Must provide exactly 6 receiver positions")
            self.receiver_positions = receiver_positions

        # Calculate axis limits from receiver positions
        receiver_x = [pos[1][0] for pos in receiver_positions]
        receiver_y = [pos[1][1] for pos in receiver_positions]
        min_x, max_x = min(receiver_x), max(receiver_x)
        min_y, max_y = min(receiver_y), max(receiver_y)

        # Initialize matplotlib figure with complex layout using GridSpec
        # Layout: Position arena on left, Run1 histograms in middle, Run2 histograms on right
        self.fig = plt.figure(figsize=(18, 10), facecolor='black')
        gs = self.fig.add_gridspec(4, 7, width_ratios=[3, 1, 1, 0.2, 1, 1, 0.2],
                                   height_ratios=[0.1, 1, 1, 1], hspace=0.3, wspace=0.3)

        # Create main arena axes (spans left column, rows 1-3)
        self.ax = self.fig.add_subplot(gs[1:4, 0])
        self.ax.set_facecolor('black')
        self.ax.set_xlim(min_x - self.grid_padding, max_x + self.grid_padding)
        self.ax.set_ylim(min_y - self.grid_padding, max_y + self.grid_padding)
        self.ax.set_aspect('equal')
        self.ax.grid(True, alpha=0.3, color='gray')
        self.ax.set_xlabel('X Position', color='white')
        self.ax.set_ylabel('Y Position', color='white')
        self.ax.set_title('Calibration Arena', color='white')
        self.ax.tick_params(colors='white')
        for spine in self.ax.spines.values():
            spine.set_color('white')

        # Initialize compass rose
        self._draw_compass_rose()

        # Draw receivers (neon magenta dots)
        receiver_x = [pos[1][0] for pos in self.receiver_positions]
        receiver_y = [pos[1][1] for pos in self.receiver_positions]
        self.ax.scatter(receiver_x, receiver_y, c='#FF00FF', s=100, zorder=5, label='Receivers')

        # Add ID labels next to each receiver+1 (to match real-life labeling)
        for receiver_id, (x, y) in self.receiver_positions:
            self.ax.text(x + 5, y + 5, str(receiver_id + 1), color='white',
                        fontsize=10, fontweight='bold', zorder=6,
                        ha='left', va='bottom')

        # Draw neon cyan line connecting receivers
        connection_order = [0, 1, 2, 5, 4, 3, 0]
        connected_x = [receiver_x[i] for i in connection_order]
        connected_y = [receiver_y[i] for i in connection_order]
        self.ax.plot(connected_x, connected_y, color='#00FFFF', linewidth=2, alpha=0.7, label='Receiver Connections')

        # Initialize vehicle position plot (neon green)
        self.vehicle_point, = self.ax.plot([], [], 'o', color='#39FF14', markersize=10, zorder=6, label='Position')
        self.vehicle_trail, = self.ax.plot([], [], '-', color='#39FF14', linewidth=1, alpha=0.5, label='Trail')

        self.ax.legend(loc='upper right', facecolor='#222222', edgecolor='white', labelcolor='white')

        # Create Run 1 histogram axes (columns 1-2, rows 1-3)
        # Layout: 3,6 top row; 2,5 middle row; 1,4 bottom row
        self.run1_axes = {}
        run1_positions = [(2, 1), (1, 2), (0, 3)]  # (receiver_id, row) for left column
        run1_positions_right = [(5, 1), (4, 2), (3, 3)]  # (receiver_id, row) for right column

        for recv_id, row in run1_positions:
            ax = self.fig.add_subplot(gs[row, 1])
            self._setup_histogram_axis(ax, f"Receiver {recv_id + 1} Serial")
            self.run1_axes[recv_id] = ax

        for recv_id, row in run1_positions_right:
            ax = self.fig.add_subplot(gs[row, 2])
            self._setup_histogram_axis(ax, f"Receiver {recv_id + 1} Serial")
            self.run1_axes[recv_id] = ax

        # Run 1 title
        run1_title_ax = self.fig.add_subplot(gs[0, 1:3])
        run1_title_ax.set_facecolor('black')
        run1_title_ax.axis('off')
        run1_title_ax.text(0.5, 0.5, 'Run 1', color='#00FFFF', fontsize=14, fontweight='bold',
                          ha='center', va='center', transform=run1_title_ax.transAxes)

        # Create Run 2 histogram axes (columns 4-5, rows 1-3)
        self.run2_axes = {}
        run2_positions = [(2, 1), (1, 2), (0, 3)]  # (receiver_id, row) for left column
        run2_positions_right = [(5, 1), (4, 2), (3, 3)]  # (receiver_id, row) for right column

        for recv_id, row in run2_positions:
            ax = self.fig.add_subplot(gs[row, 4])
            self._setup_histogram_axis(ax, f"Receiver {recv_id + 1} Serial")
            self.run2_axes[recv_id] = ax

        for recv_id, row in run2_positions_right:
            ax = self.fig.add_subplot(gs[row, 5])
            self._setup_histogram_axis(ax, f"Receiver {recv_id + 1} Serial")
            self.run2_axes[recv_id] = ax

        # Run 2 title
        run2_title_ax = self.fig.add_subplot(gs[0, 4:6])
        run2_title_ax.set_facecolor('black')
        run2_title_ax.axis('off')
        run2_title_ax.text(0.5, 0.5, 'Run 2', color='#00FFFF', fontsize=14, fontweight='bold',
                          ha='center', va='center', transform=run2_title_ax.transAxes)

        # Store histogram bar containers for updates
        self.run1_bars = {i: None for i in range(6)}
        self.run2_bars = {i: None for i in range(6)}

    def _setup_histogram_axis(self, ax, title):
        """Setup a histogram axis with consistent styling."""
        ax.set_facecolor('black')
        ax.set_title(title, color='white', fontsize=8)
        ax.tick_params(colors='white', labelsize=6)
        for spine in ax.spines.values():
            spine.set_color('white')
        ax.set_xlabel('Serial Value', color='white', fontsize=6)
        ax.set_ylabel('Count', color='white', fontsize=6)

    def update_arena(self, receiver_positions):
        """
        Update the arena dimensions and receiver positions.
        Axis limits are calculated from actual receiver positions.

        Args:
            receiver_positions (list of tuples): List of (id, (x, y)) positions for 6 receivers
        """
        self.receiver_positions = receiver_positions

        # Calculate bounds from receiver positions
        receiver_x = [pos[1][0] for pos in receiver_positions]
        receiver_y = [pos[1][1] for pos in receiver_positions]
        min_x, max_x = min(receiver_x), max(receiver_x)
        min_y, max_y = min(receiver_y), max(receiver_y)

        # Update main arena axis limits based on actual receiver positions with padding
        self.ax.set_xlim(min_x - self.grid_padding, max_x + self.grid_padding)
        self.ax.set_ylim(min_y - self.grid_padding, max_y + self.grid_padding)

        # Redraw compass rose with new axis limits
        self._draw_compass_rose()

        # Clear existing receiver scatter and labels
        for artist in self.ax.collections[:]:
            if artist.get_label() == 'Receivers':
                artist.remove()

        # Remove old receiver labels
        for text in self.ax.texts[:]:
            txt = text.get_text()
            if txt.isdigit() and 1 <= int(txt) <= 6:
                text.remove()

        # Remove old connection lines
        for line in self.ax.lines[:]:
            if line.get_label() == 'Receiver Connections':
                line.remove()

        # Redraw receivers
        self.ax.scatter(receiver_x, receiver_y, c='#FF00FF', s=100, zorder=5, label='Receivers')

        # Redraw receiver labels
        for receiver_id, (x, y) in receiver_positions:
            self.ax.text(x + 5, y + 5, str(receiver_id + 1), color='white',
                        fontsize=10, fontweight='bold', zorder=6,
                        ha='left', va='bottom')

        # Redraw connection lines
        connection_order = [0, 1, 2, 5, 4, 3, 0]
        connected_x = [receiver_x[i] for i in connection_order]
        connected_y = [receiver_y[i] for i in connection_order]
        self.ax.plot(connected_x, connected_y, color='#00FFFF', linewidth=2, alpha=0.7, label='Receiver Connections')

        # Clear position history for fresh start
        self.position_history.clear()

        # Redraw canvas if available
        if hasattr(self, 'canvas') and self.canvas:
            self.canvas.draw()

    def _draw_compass_rose(self):
        """Draw or update the X/Y compass rose in the center of the plot."""
        # Remove existing compass rose elements if they exist
        if self.compass_x_arrow:
            self.compass_x_arrow.remove()
        if self.compass_x_text:
            self.compass_x_text.remove()
        if self.compass_y_arrow:
            self.compass_y_arrow.remove()
        if self.compass_y_text:
            self.compass_y_text.remove()

        # Calculate arrow length based on current axis limits
        x_min, x_max = self.ax.get_xlim()
        y_min, y_max = self.ax.get_ylim()
        compass_x = 0
        compass_y = 0
        arrow_length = min((x_max - x_min), (y_max - y_min)) * 0.08

        # Draw X axis arrow (pointing right)
        self.compass_x_arrow = self.ax.annotate('', xy=(compass_x + arrow_length, compass_y),
                        xytext=(compass_x, compass_y),
                        arrowprops=dict(arrowstyle='->', color='white', lw=2, zorder=7))
        self.compass_x_text = self.ax.text(compass_x + arrow_length * 0.5, compass_y - arrow_length * 0.3,
                    'X', color='white', fontsize=12, fontweight='bold',
                    ha='center', va='top', zorder=7)

        # Draw Y axis arrow (pointing up)
        self.compass_y_arrow = self.ax.annotate('', xy=(compass_x, compass_y + arrow_length),
                        xytext=(compass_x, compass_y),
                        arrowprops=dict(arrowstyle='->', color='white', lw=2, zorder=7))
        self.compass_y_text = self.ax.text(compass_x - arrow_length * 0.3, compass_y + arrow_length * 0.5,
                    'Y', color='white', fontsize=12, fontweight='bold',
                    ha='right', va='center', zorder=7)

    def show_calibration_point(self, point_coords, point_num):
        """
        Display a calibration point on the arena as a green diamond.

        Args:
            point_coords: (x, y) coordinates of the calibration point
            point_num: 1 or 2 indicating which calibration point
        """
        # Remove existing calibration point display
        self.hide_calibration_point()

        x, y = point_coords
        self.cal_point_scatter = self.ax.scatter([x], [y], c='#39FF14', s=200, marker='D',
                                                  zorder=8, label=f'Cal Point {point_num}')
        self.cal_point_label = self.ax.text(x + 10, y + 10, f'Cal {point_num}',
                                            color='#39FF14', fontsize=10, fontweight='bold',
                                            zorder=8)

        if hasattr(self, 'canvas') and self.canvas:
            self.canvas.draw_idle()

    def hide_calibration_point(self):
        """Remove calibration point display from arena."""
        if self.cal_point_scatter:
            self.cal_point_scatter.remove()
            self.cal_point_scatter = None
        if self.cal_point_label:
            self.cal_point_label.remove()
            self.cal_point_label = None

    def clear_histogram_data(self, run_num):
        """Clear histogram data for a specific run."""
        if run_num == 1:
            self.run1_data = {i: {} for i in range(6)}
        elif run_num == 2:
            self.run2_data = {i: {} for i in range(6)}

    def add_serial_reading(self, receiver_id, serial_value, run_num):
        """
        Add a serial reading to the histogram data.

        Args:
            receiver_id: Receiver ID (0-5)
            serial_value: The serial value read
            run_num: 1 or 2 for which run
        """
        if run_num == 1:
            data = self.run1_data
        else:
            data = self.run2_data

        if serial_value in data[receiver_id]:
            data[receiver_id][serial_value] += 1
        else:
            data[receiver_id][serial_value] = 1

    def update_histogram(self, receiver_id, run_num):
        """
        Update the histogram display for a specific receiver.

        Args:
            receiver_id: Receiver ID (0-5)
            run_num: 1 or 2 for which run
        """
        if run_num == 1:
            ax = self.run1_axes[receiver_id]
            data = self.run1_data[receiver_id]
        else:
            ax = self.run2_axes[receiver_id]
            data = self.run2_data[receiver_id]

        # Clear existing bars
        ax.clear()
        self._setup_histogram_axis(ax, f"Receiver {receiver_id + 1} Serial")

        if not data:
            return

        # Sort by count (descending) to show most frequent at left
        sorted_data = sorted(data.items(), key=lambda x: x[1], reverse=True)
        values = [str(int(v[0])) for v in sorted_data[:10]]  # Show top 10
        counts = [v[1] for v in sorted_data[:10]]

        if values:
            bars = ax.bar(range(len(values)), counts, color='#00FFFF', alpha=0.7)
            ax.set_xticks(range(len(values)))
            ax.set_xticklabels(values, rotation=45, ha='right', fontsize=5)

            # Add label showing most common value below the plot
            most_common_value = sorted_data[0][0]  # First item is most frequent
            most_common_count = sorted_data[0][1]
            ax.set_xlabel(f'Most Common: {int(most_common_value)} (n={most_common_count})',
                         color='#39FF14', fontsize=7, fontweight='bold')

    def get_most_frequent_value(self, receiver_id, run_num):
        """
        Get the most frequent serial value for a receiver in a run.

        Args:
            receiver_id: Receiver ID (0-5)
            run_num: 1 or 2 for which run

        Returns:
            The serial value with the highest count, or None if no data
        """
        if run_num == 1:
            data = self.run1_data[receiver_id]
        else:
            data = self.run2_data[receiver_id]

        if not data:
            return None

        return max(data.items(), key=lambda x: x[1])[0]

    def get_max_count(self, receiver_id, run_num):
        """
        Get the maximum count for any serial value for a receiver in a run.

        Args:
            receiver_id: Receiver ID (0-5)
            run_num: 1 or 2 for which run

        Returns:
            The maximum count, or 0 if no data
        """
        if run_num == 1:
            data = self.run1_data[receiver_id]
        else:
            data = self.run2_data[receiver_id]

        if not data:
            return 0

        return max(data.values())

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

    def close(self):
        """Close the matplotlib window."""
        plt.close(self.fig)

class ArenaMakerWindow:
    def __init__(self, receiver_positions=None):
        """
        Initialize the ArenaMakerWindow with matplotlib.
        Contains only the X/Y position plot for arena setup purposes.

        Args:
            receiver_positions (list of tuples): List of (id, (x, y)) positions for 6 receivers.
                "id" is a 0-indexed id that denotes the tower coordinate to the tower in the real world
                The label created is index+1 to mimic real-world labels which are 1-indexed
        """
        self.grid_padding = 20  # Extra padding on the side to make receivers visible

        # Set receiver positions (6 receivers in a rectangular arrangement if not provided)
        if receiver_positions is None:
            raise ValueError("Must provide receiver positions!")
        else:
            if len(receiver_positions) != 6:
                raise ValueError("Must provide exactly 6 receiver positions")
            self.receiver_positions = receiver_positions

        # Calculate axis limits from receiver positions
        receiver_x = [pos[1][0] for pos in receiver_positions]
        receiver_y = [pos[1][1] for pos in receiver_positions]
        min_x, max_x = min(receiver_x), max(receiver_x)
        min_y, max_y = min(receiver_y), max(receiver_y)

        # Initialize matplotlib figure with single plot
        self.fig = plt.figure(figsize=(10, 10), facecolor='black')

        # Create main arena axes
        self.ax = self.fig.add_subplot(111)
        self.ax.set_facecolor('black')
        self.ax.set_xlim(min_x - self.grid_padding, max_x + self.grid_padding)
        self.ax.set_ylim(min_y - self.grid_padding, max_y + self.grid_padding)
        self.ax.set_aspect('equal')
        self.ax.grid(True, alpha=0.3, color='gray')
        self.ax.set_xlabel('X Position', color='white')
        self.ax.set_ylabel('Y Position', color='white')
        self.ax.set_title('Arena Maker', color='white')
        self.ax.tick_params(colors='white')
        for spine in self.ax.spines.values():
            spine.set_color('white')

        # Store compass rose elements for updating
        self.compass_x_arrow = None
        self.compass_x_text = None
        self.compass_y_arrow = None
        self.compass_y_text = None
        
        # Initialize compass rose
        self._draw_compass_rose()

        # Draw origin point (blue dot at 0,0)
        self.ax.scatter([0], [0], c='#0080FF', s=120, zorder=5)  # Blue
        self.ax.text(5, 5, '(0, 0)', color='#0080FF',
                    fontsize=9, fontweight='bold', zorder=6,
                    ha='left', va='bottom')

        # Note: tight_layout() removed - not needed for single subplot layouts

        # Store plot elements for updating
        self.receiver_scatter = None
        self.receiver_labels = []
        self.connection_line = None
        self.distance_labels = []  # R1-R6 distance labels
        self.origin_lines = []     # R7, R8 dashed lines from origin
        self.origin_labels = []    # R7, R8 distance labels
        self.r9_line = None        # R9 vertical dashed line (shown only if non-zero)
        self.r9_label = None       # R9 distance label
        self.r10_line = None       # R10 vertical dashed line (shown only if non-zero)
        self.r10_label = None      # R10 distance label
        self.cal_point_1_scatter = None  # Calibration point 1 marker
        self.cal_point_1_label = None    # Calibration point 1 label
        self.cal_point_2_scatter = None  # Calibration point 2 marker
        self.cal_point_2_label = None    # Calibration point 2 label
        self.rc1_line = None             # RC_1 dashed line from origin to cal_point_1
        self.rc1_label = None            # RC_1 distance label
        self.rc2_line = None             # RC_2 dashed line from origin to cal_point_2
        self.rc2_label = None            # RC_2 distance label
        self._initialize_plot_elements()

        # Create legend after plot elements are initialized (so it includes the Receivers label)
        self.ax.legend(loc='upper right', facecolor='#222222', edgecolor='white', labelcolor='white')

    def _draw_compass_rose(self):
        """Draw or update the X/Y compass rose in the center of the plot."""
        # Remove existing compass rose elements if they exist
        if self.compass_x_arrow:
            self.compass_x_arrow.remove()
        if self.compass_x_text:
            self.compass_x_text.remove()
        if self.compass_y_arrow:
            self.compass_y_arrow.remove()
        if self.compass_y_text:
            self.compass_y_text.remove()
        
        # Calculate arrow length based on current axis limits
        x_min, x_max = self.ax.get_xlim()
        y_min, y_max = self.ax.get_ylim()
        compass_x = 0
        compass_y = 0
        arrow_length = min((x_max - x_min), (y_max - y_min)) * 0.08  # 8% of smaller dimension

        # Draw X axis arrow (pointing right)
        self.compass_x_arrow = self.ax.annotate('', xy=(compass_x + arrow_length, compass_y),
                        xytext=(compass_x, compass_y),
                        arrowprops=dict(arrowstyle='->', color='white', lw=2, zorder=7))
        self.compass_x_text = self.ax.text(compass_x + arrow_length * 0.5, compass_y - arrow_length * 0.3,
                    'X', color='white', fontsize=12, fontweight='bold',
                    ha='center', va='top', zorder=7)

        # Draw Y axis arrow (pointing up)
        self.compass_y_arrow = self.ax.annotate('', xy=(compass_x, compass_y + arrow_length),
                        xytext=(compass_x, compass_y),
                        arrowprops=dict(arrowstyle='->', color='white', lw=2, zorder=7))
        self.compass_y_text = self.ax.text(compass_x - arrow_length * 0.3, compass_y + arrow_length * 0.5,
                    'Y', color='white', fontsize=12, fontweight='bold',
                    ha='right', va='center', zorder=7)

    def _initialize_plot_elements(self):
        """Initialize plot elements that can be updated."""
        receiver_x = [pos[1][0] for pos in self.receiver_positions]
        receiver_y = [pos[1][1] for pos in self.receiver_positions]

        # Store scatter plot
        scatter = self.ax.scatter(receiver_x, receiver_y, c='#FF00FF', s=100, zorder=5, label='Receivers')
        self.receiver_scatter = scatter

        # Store text labels
        self.receiver_labels = []
        for receiver_id, (x, y) in self.receiver_positions:
            label_text = f"{receiver_id + 1}\n({x:.1f}, {y:.1f})"
            label = self.ax.text(x + 5, y + 5, label_text, color='white',
                        fontsize=9, fontweight='bold', zorder=6,
                        ha='left', va='bottom')
            self.receiver_labels.append(label)

        # Store connection line
        connection_order = [0, 1, 2, 5, 4, 3, 0]
        connected_x = [receiver_x[i] for i in connection_order]
        connected_y = [receiver_y[i] for i in connection_order]
        line, = self.ax.plot(connected_x, connected_y, color='#00FFFF', linewidth=2, alpha=0.7)
        self.connection_line = line

        # Create distance labels (R1-R6)
        # R distances: R1(3→2), R2(2→1), R3(1→4), R4(4→5), R5(5→6), R6(6→3)
        # Receiver IDs: 0=Rcvr1, 1=Rcvr2, 2=Rcvr3, 3=Rcvr4, 4=Rcvr5, 5=Rcvr6
        self.distance_labels = []
        pos_dict = {p[0]: p[1] for p in self.receiver_positions}

        # Distance pairs: (name, from_id, to_id)
        distance_pairs = [
            ('R1', 2, 1),  # Receiver 3 to 2
            ('R2', 1, 0),  # Receiver 2 to 1
            ('R3', 0, 3),  # Receiver 1 to 4
            ('R4', 3, 4),  # Receiver 4 to 5
            ('R5', 4, 5),  # Receiver 5 to 6
            ('R6', 5, 2),  # Receiver 6 to 3
        ]

        for name, from_id, to_id in distance_pairs:
            x1, y1 = pos_dict[from_id]
            x2, y2 = pos_dict[to_id]
            # Midpoint
            mid_x = (x1 + x2) / 2
            mid_y = (y1 + y2) / 2
            # Calculate distance
            dist = abs(y2 - y1) if name in ['R1', 'R2', 'R4', 'R5'] else abs(x2 - x1)
            label_text = f"{name}\n{dist:.1f}"

            # Offset label to appear INSIDE the arena
            # For vertical lines (R1, R2), offset to the right (inside)
            # For vertical lines (R4, R5), offset to the left (inside)
            # For horizontal lines (R3), offset above (inside)
            # For horizontal lines (R6), offset below (inside)
            if name in ['R1', 'R2']:
                offset_x, offset_y = 15, 0   # Left side: offset right (inside)
                ha = 'left'
            elif name in ['R4', 'R5']:
                offset_x, offset_y = -15, 0  # Right side: offset left (inside)
                ha = 'right'
            elif name == 'R3':
                offset_x, offset_y = 0, 10   # Bottom: offset up (inside)
                ha = 'center'
            else:  # R6
                offset_x, offset_y = 0, -10  # Top: offset down (inside)
                ha = 'center'

            label = self.ax.text(mid_x + offset_x, mid_y + offset_y, label_text,
                        color='#FFFF00',  # Yellow for distance labels
                        fontsize=9, fontweight='bold', zorder=8,
                        ha=ha, va='center',
                        bbox=dict(boxstyle='round,pad=0.2', facecolor='black', alpha=0.7, edgecolor='#FFFF00'))
            self.distance_labels.append(label)

        # Create R7 and R8 dashed lines and labels (origin to receivers 2 and 5)
        self.origin_lines = []
        self.origin_labels = []

        # R7: Origin to Receiver 2
        x2, y2 = pos_dict[1]  # Receiver 2
        r7_line, = self.ax.plot([0, x2], [0, 0], color='#FF8800', linewidth=2, linestyle='--', zorder=4)
        self.origin_lines.append(r7_line)
        r7_dist = abs(x2)
        r7_label = self.ax.text(x2 / 2, 8, f"R7\n{r7_dist:.1f}",
                    color='#FF8800',
                    fontsize=9, fontweight='bold', zorder=8,
                    ha='center', va='bottom',
                    bbox=dict(boxstyle='round,pad=0.2', facecolor='black', alpha=0.7, edgecolor='#FF8800'))
        self.origin_labels.append(r7_label)

        # R8: Origin to Receiver 5
        x5, y5 = pos_dict[4]  # Receiver 5
        r8_line, = self.ax.plot([0, x5], [0, 0], color='#FF8800', linewidth=2, linestyle='--', zorder=4)
        self.origin_lines.append(r8_line)
        r8_dist = abs(x5)
        r8_label = self.ax.text(x5 / 2, 8, f"R8\n{r8_dist:.1f}",
                    color='#FF8800',
                    fontsize=9, fontweight='bold', zorder=8,
                    ha='center', va='bottom',
                    bbox=dict(boxstyle='round,pad=0.2', facecolor='black', alpha=0.7, edgecolor='#FF8800'))
        self.origin_labels.append(r8_label)

        # R9: Y-offset of Receiver 2 from origin (vertical dashed line, only shown if non-zero)
        # R10: Y-offset of Receiver 5 from origin (vertical dashed line, only shown if non-zero)
        self.r9_line = None
        self.r9_label = None
        self.r10_line = None
        self.r10_label = None

        # R9: Origin to Receiver 2's Y position
        if abs(y2) > 0.1:  # Only show if non-zero
            r9_line, = self.ax.plot([x2, x2], [0, y2], color='#00FF88', linewidth=2, linestyle='--', zorder=4)
            self.r9_line = r9_line
            r9_dist = y2
            r9_label = self.ax.text(x2 - 8, y2 / 2, f"R9\n{r9_dist:.1f}",
                        color='#00FF88',
                        fontsize=9, fontweight='bold', zorder=8,
                        ha='right', va='center',
                        bbox=dict(boxstyle='round,pad=0.2', facecolor='black', alpha=0.7, edgecolor='#00FF88'))
            self.r9_label = r9_label

        # R10: Origin to Receiver 5's Y position
        if abs(y5) > 0.1:  # Only show if non-zero
            r10_line, = self.ax.plot([x5, x5], [0, y5], color='#00FF88', linewidth=2, linestyle='--', zorder=4)
            self.r10_line = r10_line
            r10_dist = y5
            r10_label = self.ax.text(x5 + 8, y5 / 2, f"R10\n{r10_dist:.1f}",
                        color='#00FF88',
                        fontsize=9, fontweight='bold', zorder=8,
                        ha='left', va='center',
                        bbox=dict(boxstyle='round,pad=0.2', facecolor='black', alpha=0.7, edgecolor='#00FF88'))
            self.r10_label = r10_label

        # Initialize calibration point markers (hidden until set)
        # Cal points will be shown as green diamonds
        self.cal_point_1_scatter = None
        self.cal_point_1_label = None
        self.cal_point_2_scatter = None
        self.cal_point_2_label = None

    def update_calibration_points(self, cal_point_1, cal_point_2):
        """
        Update the calibration point markers on the plot.

        Args:
            cal_point_1: [x, y] coordinates of calibration point 1
            cal_point_2: [x, y] coordinates of calibration point 2
        """
        # Calibration point 1 (green diamond)
        x1, y1 = cal_point_1[0], cal_point_1[1]
        if self.cal_point_1_scatter is None:
            self.cal_point_1_scatter = self.ax.scatter([x1], [y1], c='#00FF00', s=150,
                                                        marker='D', zorder=6, label='Cal Points')
        else:
            self.cal_point_1_scatter.set_offsets(np.array([[x1, y1]]))
            self.cal_point_1_scatter.set_visible(True)

        if self.cal_point_1_label is None:
            self.cal_point_1_label = self.ax.text(x1 + 8, y1, f"Cal 1\n({x1:.1f}, {y1:.1f})",
                        color='#00FF00',
                        fontsize=9, fontweight='bold', zorder=6,
                        ha='left', va='center')
        else:
            self.cal_point_1_label.set_text(f"Cal 1\n({x1:.1f}, {y1:.1f})")
            self.cal_point_1_label.set_position((x1 + 8, y1))
            self.cal_point_1_label.set_visible(True)

        # Calibration point 2 (green diamond)
        x2, y2 = cal_point_2[0], cal_point_2[1]
        if self.cal_point_2_scatter is None:
            self.cal_point_2_scatter = self.ax.scatter([x2], [y2], c='#00FF00', s=150,
                                                        marker='D', zorder=6)
        else:
            self.cal_point_2_scatter.set_offsets(np.array([[x2, y2]]))
            self.cal_point_2_scatter.set_visible(True)

        if self.cal_point_2_label is None:
            self.cal_point_2_label = self.ax.text(x2 + 8, y2, f"Cal 2\n({x2:.1f}, {y2:.1f})",
                        color='#00FF00',
                        fontsize=9, fontweight='bold', zorder=6,
                        ha='left', va='center')
        else:
            self.cal_point_2_label.set_text(f"Cal 2\n({x2:.1f}, {y2:.1f})")
            self.cal_point_2_label.set_position((x2 + 8, y2))
            self.cal_point_2_label.set_visible(True)

        # RC_1: Dashed line from origin to cal_point_1
        rc1_dist = math.sqrt(x1**2 + y1**2)
        if self.rc1_line is None:
            self.rc1_line, = self.ax.plot([0, x1], [0, y1], color='#00FF00', linewidth=2,
                                           linestyle='--', zorder=4)
        else:
            self.rc1_line.set_data([0, x1], [0, y1])
            self.rc1_line.set_visible(True)

        # RC_1 label at midpoint
        rc1_mid_x, rc1_mid_y = x1 / 2, y1 / 2
        if self.rc1_label is None:
            self.rc1_label = self.ax.text(rc1_mid_x + 8, rc1_mid_y, f"RC_1\n{rc1_dist:.1f}",
                        color='#00FF00',
                        fontsize=9, fontweight='bold', zorder=8,
                        ha='left', va='center',
                        bbox=dict(boxstyle='round,pad=0.2', facecolor='black', alpha=0.7, edgecolor='#00FF00'))
        else:
            self.rc1_label.set_text(f"RC_1\n{rc1_dist:.1f}")
            self.rc1_label.set_position((rc1_mid_x + 8, rc1_mid_y))
            self.rc1_label.set_visible(True)

        # RC_2: Dashed line from origin to cal_point_2
        rc2_dist = math.sqrt(x2**2 + y2**2)
        if self.rc2_line is None:
            self.rc2_line, = self.ax.plot([0, x2], [0, y2], color='#00FF00', linewidth=2,
                                           linestyle='--', zorder=4)
        else:
            self.rc2_line.set_data([0, x2], [0, y2])
            self.rc2_line.set_visible(True)

        # RC_2 label at midpoint
        rc2_mid_x, rc2_mid_y = x2 / 2, y2 / 2
        if self.rc2_label is None:
            self.rc2_label = self.ax.text(rc2_mid_x + 8, rc2_mid_y, f"RC_2\n{rc2_dist:.1f}",
                        color='#00FF00',
                        fontsize=9, fontweight='bold', zorder=8,
                        ha='left', va='center',
                        bbox=dict(boxstyle='round,pad=0.2', facecolor='black', alpha=0.7, edgecolor='#00FF00'))
        else:
            self.rc2_label.set_text(f"RC_2\n{rc2_dist:.1f}")
            self.rc2_label.set_position((rc2_mid_x + 8, rc2_mid_y))
            self.rc2_label.set_visible(True)

        # Update legend to include calibration points
        handles, labels = self.ax.get_legend_handles_labels()
        if handles:
            self.ax.legend(handles, labels, loc='upper right', facecolor='#222222',
                          edgecolor='white', labelcolor='white')

        # Redraw canvas if available (use draw_idle to avoid stealing focus)
        if hasattr(self, 'canvas') and self.canvas:
            self.canvas.draw_idle()

    def update_arena(self, receiver_positions):
        """
        Update the arena dimensions and receiver positions.
        This is a convenience wrapper around update_receiver_positions for API consistency.

        Args:
            receiver_positions (list of tuples): List of (id, (x, y)) positions for 6 receivers
        """
        self.update_receiver_positions(receiver_positions)

    def update_receiver_positions(self, new_receiver_positions):
        """
        Update the receiver positions and redraw the plot.

        Args:
            new_receiver_positions: List of (id, (x, y)) tuples for 6 receivers
        """
        # Update stored positions
        self.receiver_positions = new_receiver_positions

        # Get new positions
        receiver_x = [pos[1][0] for pos in new_receiver_positions]
        receiver_y = [pos[1][1] for pos in new_receiver_positions]

        # Calculate actual bounds from receiver positions
        min_x, max_x = min(receiver_x), max(receiver_x)
        min_y, max_y = min(receiver_y), max(receiver_y)

        # Update axis limits based on actual receiver positions with padding
        self.ax.set_xlim(min_x - self.grid_padding, max_x + self.grid_padding)
        self.ax.set_ylim(min_y - self.grid_padding, max_y + self.grid_padding)

        # Redraw compass rose with new axis limits
        self._draw_compass_rose()
        
        # Update scatter plot offsets if it exists
        if self.receiver_scatter:
            import numpy as np
            offsets = np.column_stack([receiver_x, receiver_y])
            self.receiver_scatter.set_offsets(offsets)
            # Ensure label is set
            if not self.receiver_scatter.get_label() or self.receiver_scatter.get_label().startswith('_'):
                self.receiver_scatter.set_label('Receivers')
        else:
            # Create scatter plot if it doesn't exist
            self.receiver_scatter = self.ax.scatter(receiver_x, receiver_y, c='#FF00FF', s=100, zorder=5, label='Receivers')
        
        # Update legend to include the scatter plot
        handles, labels = self.ax.get_legend_handles_labels()
        if handles:
            self.ax.legend(handles, labels, loc='upper right', facecolor='#222222', edgecolor='white', labelcolor='white')
        
        # Update text labels
        for i, (receiver_id, (x, y)) in enumerate(new_receiver_positions):
            label_text = f"{receiver_id + 1}\n({x:.1f}, {y:.1f})"
            if i < len(self.receiver_labels):
                # Update existing label
                self.receiver_labels[i].set_text(label_text)
                self.receiver_labels[i].set_position((x + 5, y + 5))
            else:
                # Create new label if needed
                label = self.ax.text(x + 5, y + 5, label_text, color='white',
                            fontsize=9, fontweight='bold', zorder=6,
                            ha='left', va='bottom')
                self.receiver_labels.append(label)
        
        # Update connection line
        connection_order = [0, 1, 2, 5, 4, 3, 0]
        connected_x = [receiver_x[i] for i in connection_order]
        connected_y = [receiver_y[i] for i in connection_order]

        if self.connection_line:
            self.connection_line.set_data(connected_x, connected_y)
        else:
            self.connection_line, = self.ax.plot(connected_x, connected_y, color='#00FFFF', linewidth=2, alpha=0.7)

        # Update distance labels (R1-R6)
        pos_dict = {p[0]: p[1] for p in new_receiver_positions}
        distance_pairs = [
            ('R1', 2, 1),  # Receiver 3 to 2
            ('R2', 1, 0),  # Receiver 2 to 1
            ('R3', 0, 3),  # Receiver 1 to 4
            ('R4', 3, 4),  # Receiver 4 to 5
            ('R5', 4, 5),  # Receiver 5 to 6
            ('R6', 5, 2),  # Receiver 6 to 3
        ]

        for i, (name, from_id, to_id) in enumerate(distance_pairs):
            x1, y1 = pos_dict[from_id]
            x2, y2 = pos_dict[to_id]
            # Midpoint
            mid_x = (x1 + x2) / 2
            mid_y = (y1 + y2) / 2
            # Calculate distance (vertical for R1,R2,R4,R5; horizontal for R3,R6)
            dist = abs(y2 - y1) if name in ['R1', 'R2', 'R4', 'R5'] else abs(x2 - x1)
            label_text = f"{name}\n{dist:.1f}"

            # Offset label to appear INSIDE the arena
            if name in ['R1', 'R2']:
                offset_x, offset_y = 15, 0   # Left side: offset right (inside)
                ha = 'left'
            elif name in ['R4', 'R5']:
                offset_x, offset_y = -15, 0  # Right side: offset left (inside)
                ha = 'right'
            elif name == 'R3':
                offset_x, offset_y = 0, 10   # Bottom: offset up (inside)
                ha = 'center'
            else:  # R6
                offset_x, offset_y = 0, -10  # Top: offset down (inside)
                ha = 'center'

            if i < len(self.distance_labels):
                # Update existing label
                self.distance_labels[i].set_text(label_text)
                self.distance_labels[i].set_position((mid_x + offset_x, mid_y + offset_y))
            else:
                # Create new label if needed
                label = self.ax.text(mid_x + offset_x, mid_y + offset_y, label_text,
                            color='#FFFF00',
                            fontsize=9, fontweight='bold', zorder=8,
                            ha=ha, va='center',
                            bbox=dict(boxstyle='round,pad=0.2', facecolor='black', alpha=0.7, edgecolor='#FFFF00'))
                self.distance_labels.append(label)

        # Update R7 and R8 dashed lines and labels
        x2, y2 = pos_dict[1]  # Receiver 2
        x5, y5 = pos_dict[4]  # Receiver 5

        # R7: Origin to Receiver 2
        r7_dist = abs(x2)
        if len(self.origin_lines) > 0:
            self.origin_lines[0].set_data([0, x2], [0, 0])
        else:
            r7_line, = self.ax.plot([0, x2], [0, 0], color='#FF8800', linewidth=2, linestyle='--', zorder=4)
            self.origin_lines.append(r7_line)

        if len(self.origin_labels) > 0:
            self.origin_labels[0].set_text(f"R7\n{r7_dist:.1f}")
            self.origin_labels[0].set_position((x2 / 2, 8))
        else:
            r7_label = self.ax.text(x2 / 2, 8, f"R7\n{r7_dist:.1f}",
                        color='#FF8800',
                        fontsize=9, fontweight='bold', zorder=8,
                        ha='center', va='bottom',
                        bbox=dict(boxstyle='round,pad=0.2', facecolor='black', alpha=0.7, edgecolor='#FF8800'))
            self.origin_labels.append(r7_label)

        # R8: Origin to Receiver 5
        r8_dist = abs(x5)
        if len(self.origin_lines) > 1:
            self.origin_lines[1].set_data([0, x5], [0, 0])
        else:
            r8_line, = self.ax.plot([0, x5], [0, 0], color='#FF8800', linewidth=2, linestyle='--', zorder=4)
            self.origin_lines.append(r8_line)

        if len(self.origin_labels) > 1:
            self.origin_labels[1].set_text(f"R8\n{r8_dist:.1f}")
            self.origin_labels[1].set_position((x5 / 2, 8))
        else:
            r8_label = self.ax.text(x5 / 2, 8, f"R8\n{r8_dist:.1f}",
                        color='#FF8800',
                        fontsize=9, fontweight='bold', zorder=8,
                        ha='center', va='bottom',
                        bbox=dict(boxstyle='round,pad=0.2', facecolor='black', alpha=0.7, edgecolor='#FF8800'))
            self.origin_labels.append(r8_label)

        # Update R9: Y-offset of Receiver 2 (only shown if non-zero)
        r9_val = y2  # y2 is receiver 2's y-position (which is R9)
        if abs(r9_val) > 0.1:
            # Show R9 line and label
            if self.r9_line is None:
                r9_line, = self.ax.plot([x2, x2], [0, r9_val], color='#00FF88', linewidth=2, linestyle='--', zorder=4)
                self.r9_line = r9_line
            else:
                self.r9_line.set_data([x2, x2], [0, r9_val])
                self.r9_line.set_visible(True)

            if self.r9_label is None:
                r9_label = self.ax.text(x2 - 8, r9_val / 2, f"R9\n{r9_val:.1f}",
                            color='#00FF88',
                            fontsize=9, fontweight='bold', zorder=8,
                            ha='right', va='center',
                            bbox=dict(boxstyle='round,pad=0.2', facecolor='black', alpha=0.7, edgecolor='#00FF88'))
                self.r9_label = r9_label
            else:
                self.r9_label.set_text(f"R9\n{r9_val:.1f}")
                self.r9_label.set_position((x2 - 8, r9_val / 2))
                self.r9_label.set_visible(True)
        else:
            # Hide R9 line and label if value is zero
            if self.r9_line is not None:
                self.r9_line.set_visible(False)
            if self.r9_label is not None:
                self.r9_label.set_visible(False)

        # Update R10: Y-offset of Receiver 5 (only shown if non-zero)
        r10_val = y5  # y5 is receiver 5's y-position (which is R10)
        if abs(r10_val) > 0.1:
            # Show R10 line and label
            if self.r10_line is None:
                r10_line, = self.ax.plot([x5, x5], [0, r10_val], color='#00FF88', linewidth=2, linestyle='--', zorder=4)
                self.r10_line = r10_line
            else:
                self.r10_line.set_data([x5, x5], [0, r10_val])
                self.r10_line.set_visible(True)

            if self.r10_label is None:
                r10_label = self.ax.text(x5 + 8, r10_val / 2, f"R10\n{r10_val:.1f}",
                            color='#00FF88',
                            fontsize=9, fontweight='bold', zorder=8,
                            ha='left', va='center',
                            bbox=dict(boxstyle='round,pad=0.2', facecolor='black', alpha=0.7, edgecolor='#00FF88'))
                self.r10_label = r10_label
            else:
                self.r10_label.set_text(f"R10\n{r10_val:.1f}")
                self.r10_label.set_position((x5 + 8, r10_val / 2))
                self.r10_label.set_visible(True)
        else:
            # Hide R10 line and label if value is zero
            if self.r10_line is not None:
                self.r10_line.set_visible(False)
            if self.r10_label is not None:
                self.r10_label.set_visible(False)

        # Redraw canvas if available (use draw_idle to avoid stealing focus)
        if hasattr(self, 'canvas') and self.canvas:
            self.canvas.draw_idle()

    def close(self):
        """Close the matplotlib window."""
        plt.close(self.fig)

class MainWindow:
    def __init__(self):
        """
        Initialize the MainWindow - the main menu for the application.
        Contains title "Ultra GPS Control" and navigation buttons.

        This is a tkinter-only window (no matplotlib).
        The frame and widgets are created when build() is called with a parent frame.
        """
        self.frame = None
        self.title_label = None
        self.position_button = None
        self.calibration_button = None
        self.setup_button = None

        # Button callbacks (set by GraphicsModule)
        self.on_position_click = None
        self.on_calibration_click = None
        self.on_setup_click = None

    def build(self, parent):
        """
        Build the MainWindow UI elements inside the given parent frame.

        Args:
            parent: The parent tkinter frame to build into
        """
        self.frame = parent

        # Create a container frame for centering content
        center_container = tk.Frame(self.frame, bg='black')
        center_container.place(relx=0.5, rely=0.5, anchor=tk.CENTER)

        # Title label
        self.title_label = tk.Label(
            center_container,
            text="Ultra GPS Control",
            bg='black',
            fg='#39FF14',  # Neon green
            font=('Arial', 32, 'bold')
        )
        self.title_label.pack(pady=(0, 40))

        # Button style configuration
        button_config = {
            'font': ('Arial', 16, 'bold'),
            'width': 20,
            'height': 2,
            'relief': tk.RAISED,
            'bd': 3,
            'padx': 20,
            'pady': 10
        }

        # Position button
        self.position_button = tk.Button(
            center_container,
            text="Position",
            command=self._on_position_click,
            bg='#39FF14',  # Neon green
            fg='black',
            activebackground='#2BCC10',
            activeforeground='black',
            **button_config
        )
        self.position_button.pack(pady=10)

        # Calibration button
        self.calibration_button = tk.Button(
            center_container,
            text="Calibration",
            command=self._on_calibration_click,
            bg='#FF00FF',  # Neon magenta
            fg='white',
            activebackground='#CC00CC',
            activeforeground='white',
            **button_config
        )
        self.calibration_button.pack(pady=10)

        # Setup button (goes to Arena Maker)
        self.setup_button = tk.Button(
            center_container,
            text="Setup",
            command=self._on_setup_click,
            bg='#00FFFF',  # Neon cyan
            fg='black',
            activebackground='#00CCCC',
            activeforeground='black',
            **button_config
        )
        self.setup_button.pack(pady=10)

    def _on_position_click(self):
        """Handle Position button click."""
        if self.on_position_click:
            self.on_position_click()

    def _on_calibration_click(self):
        """Handle Calibration button click."""
        if self.on_calibration_click:
            self.on_calibration_click()

    def _on_setup_click(self):
        """Handle Setup button click."""
        if self.on_setup_click:
            self.on_setup_click()

    def close(self):
        """Clean up resources."""
        pass  # No matplotlib resources to clean up
