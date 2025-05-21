import tkinter as tk
import tkinter.simpledialog
import tkinter.messagebox
from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ConnectionException, ModbusException
from PIL import Image, ImageTk
import sys
import os
import logging
import threading
import time
import json
from datetime import datetime, timedelta
import subprocess
import platform
import sqlite3
import atexit

# Set up logging for the application (file and console)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('tank_level_gui.log'),
        logging.StreamHandler()
    ]
)

# Reduce pymodbus logging verbosity
logging.getLogger('pymodbus').setLevel(logging.WARNING)

# Configuration
CAPACITIES = {
    'Big Tank': {'gallons': 582000, 'height': 52.6},
    'Tank 1 - Bio': {'gallons': 20000, 'height': 25},
    'Tank 2 - Fleet': {'gallons': 17478.68, 'height': 31},
    'Tank 3 - Fleet': {'gallons': 17478.68, 'height': 31},
    'Tank 4 - Bio': {'gallons': 20000, 'height': 25},
    'Tank 5 - Bio': {'gallons': 20000, 'height': 25}
}
BIG_TANK_HEEL = 21266
GALLONS_PER_BBL = 42
LOGO_FILE_PATH = "benchmark_logo.jpg"
MIN_POLL_INTERVAL = 0.1  # Minimum polling interval in seconds
MODBUS_TIMEOUT = 5  # Timeout for Modbus operations in seconds
DEFAULT_POLL_INTERVAL = 60  # Default polling interval in seconds

def resource_path(relative_path):
    """Get the absolute path to a resource, works for dev and for PyInstaller."""
    try:
        if hasattr(sys, '_MEIPASS'):
            base_path = sys._MEIPASS
            logging.debug("Running in PyInstaller bundle, using _MEIPASS: %s", base_path)
        else:
            base_path = os.path.abspath(".")
            logging.debug("Running in development mode, using current directory: %s", base_path)
        full_path = os.path.join(base_path, relative_path)
        logging.debug("Resolved path for %s: %s", relative_path, full_path)
        return full_path
    except Exception as e:
        logging.error("Error resolving resource path for %s: %s", relative_path, e)
        raise

def load_config():
    """Load configuration from config.json, create or update to default if needed."""
    config_path = resource_path('config.json')
    default_config = {'adam_ip': '192.168.1.235', 'poll_interval': DEFAULT_POLL_INTERVAL, 'unit_id': 1}
    
    # If config.json doesn't exist, create it with default values
    if not os.path.exists(config_path):
        try:
            with open(config_path, 'w') as f:
                json.dump(default_config, f, indent=4)
            logging.info("Created default config.json at: %s", config_path)
        except Exception as e:
            logging.error("Failed to create default config.json: %s", e)
            return default_config
    
    # Load the existing config
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
            logging.info("Loaded configuration: %s", config)
            # Check if poll_interval is not the desired default (60 seconds)
            if config.get('poll_interval') != DEFAULT_POLL_INTERVAL:
                logging.info("Updating poll_interval in config.json from %s to %s", config['poll_interval'], DEFAULT_POLL_INTERVAL)
                config['poll_interval'] = DEFAULT_POLL_INTERVAL
                try:
                    with open(config_path, 'w') as f:
                        json.dump(config, f, indent=4)
                    logging.info("Updated config.json with default poll_interval: %s", DEFAULT_POLL_INTERVAL)
                except Exception as e:
                    logging.error("Failed to update config.json: %s", e)
            return config
    except Exception as e:
        logging.error("Failed to load config: %s", e)
        return default_config

class SettingsWindow:
    """Window for configuring IP and polling interval at runtime."""
    def __init__(self, parent, app):
        self.app = app
        self.window = tk.Toplevel(parent)
        self.window.title("Settings")

        # Center the window on the screen
        window_width = 300
        window_height = 200
        screen_width = self.window.winfo_screenwidth()
        screen_height = self.window.winfo_screenheight()
        x = (screen_width - window_width) // 2
        y = (screen_height - window_height) // 2
        self.window.geometry(f"{window_width}x{window_height}+{x}+{y}")

        tk.Label(self.window, text="ADAM-6017 IP:").pack(pady=5)
        self.ip_entry = tk.Entry(self.window)
        self.ip_entry.insert(0, app.adam_ip)
        self.ip_entry.pack()
        tk.Label(self.window, text="Polling Interval (s):").pack(pady=5)
        self.interval_entry = tk.Entry(self.window)
        self.interval_entry.insert(0, str(app.poll_interval))
        self.interval_entry.pack()
        tk.Button(self.window, text="Save", command=self.save).pack(pady=10)

    def save(self):
        try:
            new_ip = self.ip_entry.get()
            new_interval = float(self.interval_entry.get())
            if new_interval < MIN_POLL_INTERVAL:
                raise ValueError(f"Polling interval must be at least {MIN_POLL_INTERVAL} seconds")
            self.app.adam_ip = new_ip
            self.app.poll_interval = new_interval
            self.app.client.host = new_ip
            logging.info("Updated settings: IP=%s, Interval=%s", new_ip, new_interval)
            self.window.destroy()
        except ValueError as e:
            tk.messagebox.showerror("Error", str(e))
            logging.error("Invalid input: %s", e)

class TankLevelGUI:
    def __init__(self, root):
        logging.info("Starting TankLevelGUI.__init__")
        self.root = root
        self.root.title("Fuel Terminal Tank Levels")
        self.root.geometry("1300x900")  # Increased height from 700 to 900
        self.root.resizable(True, True)  # Allow window to be resizable
        self.root.configure(bg="white")
        self.running = True
        self.last_update = None
        self.stale_threshold = timedelta(seconds=10)
        self.time_date_after_id = None
        self.cleaned_up = False  # Initialize cleaned_up attribute
        logging.info("Initialized basic Tkinter window properties")

        # Load configuration
        config = load_config()
        self.adam_ip = config['adam_ip']
        self.poll_interval = max(float(config['poll_interval']), MIN_POLL_INTERVAL)
        self.unit_id = config['unit_id']
        logging.info("Loaded configuration: adam_ip=%s, poll_interval=%s, unit_id=%s", self.adam_ip, self.poll_interval, self.unit_id)

        # Initialize Modbus client
        try:
            self.client = ModbusTcpClient(self.adam_ip, port=502, timeout=MODBUS_TIMEOUT)
            logging.info("Initialized Modbus client")
        except Exception as e:
            logging.error("Failed to initialize Modbus client: %s", e)
            self.client = None  # Set to None to prevent further errors

        # Initialize SQLite database
        self.db_path = resource_path('tank_levels.db')
        # Delete existing database file to ensure a fresh start
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
                logging.info("Deleted existing database file: %s", self.db_path)
            except Exception as e:
                logging.error("Failed to delete existing database file %s: %s", self.db_path, e)
        self.setup_database()
        logging.info("Set up SQLite database")

        # Register cleanup function for unexpected exits
        atexit.register(self.cleanup)
        logging.info("Registered cleanup function")

        self.tank_names = {
            'Big Tank': ('Big Tank', 'Diesel'),
            'Tank 1 - Bio': ('Tank #1', 'BioDiesel'),
            'Tank 2 - Fleet': ('Tank #2', 'Fleet Fuel'),
            'Tank 3 - Fleet': ('Tank #3', 'Fleet Fuel'),
            'Tank 4 - Bio': ('Tank #4', 'BioDiesel'),
            'Tank 5 - Bio': ('Tank #5', 'BioDiesel')
        }

        self.tank_levels = [
            {'id': 'Big Tank', 'level': 0, 'capacity': CAPACITIES['Big Tank']['gallons'], 'height': CAPACITIES['Big Tank']['height']},
            {'id': 'Tank 1 - Bio', 'level': 0, 'capacity': CAPACITIES['Tank 1 - Bio']['gallons'], 'height': CAPACITIES['Tank 1 - Bio']['height']},
            {'id': 'Tank 2 - Fleet', 'level': 0, 'capacity': CAPACITIES['Tank 2 - Fleet']['gallons'], 'height': CAPACITIES['Tank 2 - Fleet']['height']},
            {'id': 'Tank 3 - Fleet', 'level': 0, 'capacity': CAPACITIES['Tank 3 - Fleet']['gallons'], 'height': CAPACITIES['Tank 3 - Fleet']['height']},
            {'id': 'Tank 4 - Bio', 'level': 0, 'capacity': CAPACITIES['Tank 4 - Bio']['gallons'], 'height': CAPACITIES['Tank 4 - Bio']['height']},
            {'id': 'Tank 5 - Bio', 'level': 0, 'capacity': CAPACITIES['Tank 5 - Bio']['gallons'], 'height': CAPACITIES['Tank 5 - Bio']['height']}
        ]
        logging.info("Defined tank names and levels")

        # Create menu bar
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)

        # Add Settings menu
        settings_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Settings", menu=settings_menu)
        settings_menu.add_command(label="Configure", command=lambda: SettingsWindow(self.root, self))
        logging.info("Created menu bar")

        # Add time and date label in the top left corner
        self.time_date_var = tk.StringVar()
        self.update_time_date()  # Initial update
        time_date_label = tk.Label(self.root, textvariable=self.time_date_var, font=("Arial", 12), bg="white")
        time_date_label.place(x=10, y=10, anchor="nw")  # Top left with 10px padding
        logging.info("Added the time and date label")

        # Main frame for tanks
        self.main_frame = tk.Frame(root, bg="white")
        self.main_frame.pack(fill=tk.BOTH, expand=True)

        # Tank frame at the top
        self.tank_frame = tk.Frame(self.main_frame, bg="white")
        self.tank_frame.pack(side=tk.TOP, pady=50)
        logging.info("Created main and tank frames")

        self.tank_displays = {}

        # Big Tank (Column 0)
        big_tank = self.tank_levels[0]
        big_container = tk.Frame(self.tank_frame, bg="white")
        big_container.grid(row=0, column=0, padx=20, sticky="s")
        tk.Label(big_container, text=self.tank_names[big_tank['id']][0], font=("Arial", 14, "bold"), bg="white").pack()
        big_box = tk.Frame(big_container, width=300, height=450, borderwidth=2, relief="raised", bg="#ADD8E6")
        big_box.pack()
        tk.Label(big_box, text=self.tank_names[big_tank['id']][1], font=("Arial", 14, "bold"), bg="#ADD8E6").pack(pady=5)
        big_canvas = tk.Canvas(big_box, width=260, height=350, bg="#a9a9a9", highlightthickness=0)
        big_canvas.pack()
        big_fill = big_canvas.create_rectangle(0, 350, 260, 350, fill="#32CD32")
        big_height_var = tk.StringVar(value="0 ft 0 in")
        big_height_box = tk.Frame(big_canvas, bg="#a9a9a9", borderwidth=1, relief="solid")
        tk.Label(big_height_box, textvariable=big_height_var, font=("Arial", 8), bg="#a9a9a9").pack()
        big_height_window = big_canvas.create_window(130, 350, window=big_height_box)
        self.add_scale(big_canvas, big_tank['height'], 350, 260)
        self.tank_displays[big_tank['id']] = {
            'canvas': big_canvas, 'fill_bar': big_fill, 'height_var': big_height_var, 'height_window': big_height_window
        }
        logging.info("Created Big Tank display")

        # Tank 1 - Bio (Column 1)
        tank1_bio = self.tank_levels[1]
        tank1_container = tk.Frame(self.tank_frame, bg="white")
        tank1_container.grid(row=0, column=1, padx=20, sticky="s")
        tk.Label(tank1_container, text=self.tank_names[tank1_bio['id']][0], font=("Arial", 10, "bold"), bg="white").pack()
        tank1_box = tk.Frame(tank1_container, width=170, height=300, borderwidth=2, relief="raised", bg="#ADD8E6")
        tank1_box.pack()
        tk.Label(tank1_box, text=self.tank_names[tank1_bio['id']][1], font=("Arial", 10, "bold"), bg="#ADD8E6").pack(pady=5)
        tank1_canvas = tk.Canvas(tank1_box, width=130, height=220, bg="#a9a9a9", highlightthickness=0)
        tank1_canvas.pack()
        tank1_fill = tank1_canvas.create_rectangle(0, 220, 130, 220, fill="#FFA500")
        tank1_height_var = tk.StringVar(value="0 ft 0 in")
        tank1_height_box = tk.Frame(tank1_canvas, bg="#a9a9a9", borderwidth=1, relief="solid")
        tk.Label(tank1_height_box, textvariable=tank1_height_var, font=("Arial", 8), bg="#a9a9a9").pack()
        tank1_height_window = tank1_canvas.create_window(65, 220, window=tank1_height_box)
        self.add_scale(tank1_canvas, tank1_bio['height'], 220, 130)
        self.tank_displays[tank1_bio['id']] = {
            'canvas': tank1_canvas, 'fill_bar': tank1_fill, 'height_var': tank1_height_var, 'height_window': tank1_height_window
        }
        logging.info("Created Tank 1 display")

        # Tank 2 - Fleet (Column 2)
        tank2_fleet = self.tank_levels[2]
        tank2_container = tk.Frame(self.tank_frame, bg="white")
        tank2_container.grid(row=0, column=2, padx=20, sticky="s")
        tk.Label(tank2_container, text=self.tank_names[tank2_fleet['id']][0], font=("Arial", 10, "bold"), bg="white").pack()
        tank2_box = tk.Frame(tank2_container, width=150, height=350, borderwidth=2, relief="raised", bg="#ADD8E6")
        tank2_box.pack()
        tk.Label(tank2_box, text=self.tank_names[tank2_fleet['id']][1], font=("Arial", 10, "bold"), bg="#ADD8E6").pack(pady=5)
        tank2_canvas = tk.Canvas(tank2_box, width=110, height=270, bg="#a9a9a9", highlightthickness=0)
        tank2_canvas.pack()
        tank2_fill = tank2_canvas.create_rectangle(0, 270, 110, 270, fill="#FFFF00")
        tank2_height_var = tk.StringVar(value="0 ft 0 in")
        tank2_height_box = tk.Frame(tank2_canvas, bg="#a9a9a9", borderwidth=1, relief="solid")
        tk.Label(tank2_height_box, textvariable=tank2_height_var, font=("Arial", 8), bg="#a9a9a9").pack()
        tank2_height_window = tank2_canvas.create_window(55, 270, window=tank2_height_box)
        self.add_scale(tank2_canvas, tank2_fleet['height'], 270, 110)
        self.tank_displays[tank2_fleet['id']] = {
            'canvas': tank2_canvas, 'fill_bar': tank2_fill, 'height_var': tank2_height_var, 'height_window': tank2_height_window
        }
        logging.info("Created Tank 2 display")

        # Tank 3 - Fleet (Column 3)
        tank3_fleet = self.tank_levels[3]
        tank3_container = tk.Frame(self.tank_frame, bg="white")
        tank3_container.grid(row=0, column=3, padx=20, sticky="s")
        tk.Label(tank3_container, text=self.tank_names[tank3_fleet['id']][0], font=("Arial", 10, "bold"), bg="white").pack()
        tank3_box = tk.Frame(tank3_container, width=150, height=350, borderwidth=2, relief="raised", bg="#ADD8E6")
        tank3_box.pack()
        tk.Label(tank3_box, text=self.tank_names[tank3_fleet['id']][1], font=("Arial", 10, "bold"), bg="#ADD8E6").pack(pady=5)
        tank3_canvas = tk.Canvas(tank3_box, width=110, height=270, bg="#a9a9a9", highlightthickness=0)
        tank3_canvas.pack()
        tank3_fill = tank3_canvas.create_rectangle(0, 270, 110, 270, fill="#FFFF00")
        tank3_height_var = tk.StringVar(value="0 ft 0 in")
        tank3_height_box = tk.Frame(tank3_canvas, bg="#a9a9a9", borderwidth=1, relief="solid")
        tk.Label(tank3_height_box, textvariable=tank3_height_var, font=("Arial", 8), bg="#a9a9a9").pack()
        tank3_height_window = tank3_canvas.create_window(55, 270, window=tank3_height_box)
        self.add_scale(tank3_canvas, tank3_fleet['height'], 270, 110)
        self.tank_displays[tank3_fleet['id']] = {
            'canvas': tank3_canvas, 'fill_bar': tank3_fill, 'height_var': tank3_height_var, 'height_window': tank3_height_window
        }
        logging.info("Created Tank 3 display")

        # Tank 4 - Bio (Column 4)
        tank4_bio = self.tank_levels[4]
        tank4_container = tk.Frame(self.tank_frame, bg="white")
        tank4_container.grid(row=0, column=4, padx=20, sticky="s")
        tk.Label(tank4_container, text=self.tank_names[tank4_bio['id']][0], font=("Arial", 10, "bold"), bg="white").pack()
        tank4_box = tk.Frame(tank4_container, width=170, height=300, borderwidth=2, relief="raised", bg="#ADD8E6")
        tank4_box.pack()
        tk.Label(tank4_box, text=self.tank_names[tank4_bio['id']][1], font=("Arial", 10, "bold"), bg="#ADD8E6").pack(pady=5)
        tank4_canvas = tk.Canvas(tank4_box, width=130, height=220, bg="#a9a9a9", highlightthickness=0)
        tank4_canvas.pack()
        tank4_fill = tank4_canvas.create_rectangle(0, 220, 130, 220, fill="#FFA500")
        tank4_height_var = tk.StringVar(value="0 ft 0 in")
        tank4_height_box = tk.Frame(tank4_canvas, bg="#a9a9a9", borderwidth=1, relief="solid")
        tk.Label(tank4_height_box, textvariable=tank4_height_var, font=("Arial", 8), bg="#a9a9a9").pack()
        tank4_height_window = tank4_canvas.create_window(65, 220, window=tank4_height_box)
        self.add_scale(tank4_canvas, tank4_bio['height'], 220, 130)
        self.tank_displays[tank4_bio['id']] = {
            'canvas': tank4_canvas, 'fill_bar': tank4_fill, 'height_var': tank4_height_var, 'height_window': tank4_height_window
        }
        logging.info("Created Tank 4 display")

        # Tank 5 - Bio (Column 5)
        tank5_bio = self.tank_levels[5]
        tank5_container = tk.Frame(self.tank_frame, bg="white")
        tank5_container.grid(row=0, column=5, padx=20, sticky="s")
        tk.Label(tank5_container, text=self.tank_names[tank5_bio['id']][0], font=("Arial", 10, "bold"), bg="white").pack()
        tank5_box = tk.Frame(tank5_container, width=170, height=300, borderwidth=2, relief="raised", bg="#ADD8E6")
        tank5_box.pack()
        tk.Label(tank5_box, text=self.tank_names[tank5_bio['id']][1], font=("Arial", 10, "bold"), bg="#ADD8E6").pack(pady=5)
        tank5_canvas = tk.Canvas(tank5_box, width=130, height=220, bg="#a9a9a9", highlightthickness=0)
        tank5_canvas.pack()
        tank5_fill = tank5_canvas.create_rectangle(0, 220, 130, 220, fill="#FFA500")
        tank5_height_var = tk.StringVar(value="0 ft 0 in")
        tank5_height_box = tk.Frame(tank5_canvas, bg="#a9a9a9", borderwidth=1, relief="solid")
        tk.Label(tank5_height_box, textvariable=tank5_height_var, font=("Arial", 8), bg="#a9a9a9").pack()
        tank5_height_window = tank5_canvas.create_window(65, 220, window=tank5_height_box)
        self.add_scale(tank5_canvas, tank5_bio['height'], 220, 130)
        self.tank_displays[tank5_bio['id']] = {
            'canvas': tank5_canvas, 'fill_bar': tank5_fill, 'height_var': tank5_height_var, 'height_window': tank5_height_window
        }
        logging.info("Created Tank 5 display")

        # Spacer row with reduced height
        for col in range(6):
            tk.Label(self.tank_frame, text="", bg="white", height=1).grid(row=1, column=col)
        logging.info("Added spacer row")

        # Big Tank Labels
        # Total Gallons (Row 2)
        big_total_gallons_var = tk.StringVar(value=f"Total Gallons: {int(big_tank['level']):,}")
        big_total_gallons_label = tk.Label(self.tank_frame, textvariable=big_total_gallons_var, font=("Arial", 12), bg="white")
        big_total_gallons_label.grid(row=2, column=0, padx=20)

        # Heel Label (Row 3)
        heel_label = tk.Label(self.tank_frame, text="Heel: 24 inches = 21,266 gallons", font=("Arial", 9), bg="white")
        heel_label.grid(row=3, column=0, padx=20)

        # Usable Gallons (Row 4)
        big_usable_gallons_var = tk.StringVar(value=f"Usable Gallons: {max(0, int(big_tank['level'] - BIG_TANK_HEEL)):,}")
        big_usable_gallons_label = tk.Label(self.tank_frame, textvariable=big_usable_gallons_var, font=("Arial", 12), bg="#FFFF00", bd=2, relief="solid", highlightbackground="green", highlightcolor="green", highlightthickness=2)
        big_usable_gallons_label.grid(row=4, column=0, padx=20)

        # TANK CAPACITY = 12,967 Barrels (Row 5, Reduced Font Size)
        tk.Label(self.tank_frame, text="TANK CAPACITY = 12,967 Barrels", font=("Arial", 10), bg="white").grid(row=5, column=0, padx=20)

        # USABLE CAPACITY = 12,495 Barrels (Row 6, Reduced Font Size)
        tk.Label(self.tank_frame, text="USABLE CAPACITY = 12,495 Barrels", font=("Arial", 10), bg="white").grid(row=6, column=0, padx=20)

        # BARRELS IN-TANK (Row 7, Dynamic with Gray Background, Reduced Font Size)
        barrels_in_tank_frame = tk.Frame(self.tank_frame, bg="white")
        barrels_in_tank_frame.grid(row=7, column=0, padx=20)
        tk.Label(barrels_in_tank_frame, text="BARRELS IN-TANK =", font=("Arial", 10), bg="white").pack(side=tk.LEFT)
        self.big_barrels_in_tank_var = tk.StringVar(value="0")
        tk.Label(barrels_in_tank_frame, textvariable=self.big_barrels_in_tank_var, font=("Arial", 10), bg="gray").pack(side=tk.LEFT)

        # ROOM SHEET in BBLs (Row 8, Turquoise Green Background, Reduced Font Size)
        self.big_room_sheet_var = tk.StringVar(value="0")
        tk.Label(self.tank_frame, textvariable=self.big_room_sheet_var, font=("Arial", 10), bg="#40E0D0").grid(row=8, column=0, padx=20)

        # Update tank_displays with the remaining variables
        self.tank_displays[big_tank['id']]['total_gallons_var'] = big_total_gallons_var
        self.tank_displays[big_tank['id']]['usable_gallons_var'] = big_usable_gallons_var
        self.tank_displays[big_tank['id']]['barrels_in_tank_var'] = self.big_barrels_in_tank_var
        self.tank_displays[big_tank['id']]['room_sheet_var'] = self.big_room_sheet_var
        logging.info("Added Big Tank labels")

        # Tank 1 Label
        tank1_level_var = tk.StringVar(value=f"{int(tank1_bio['level']):,} gal")
        tk.Label(self.tank_frame, textvariable=tank1_level_var, font=("Arial", 8), bg="white").grid(row=2, column=1, padx=20)
        tk.Label(self.tank_frame, text="", font=("Arial", 12), bg="white").grid(row=3, column=1, padx=20)
        tk.Label(self.tank_frame, text="", font=("Arial", 10), bg="white").grid(row=4, column=1, padx=20)
        tk.Label(self.tank_frame, text="", font=("Arial", 10), bg="white").grid(row=5, column=1, padx=20)
        tk.Label(self.tank_frame, text="", font=("Arial", 10), bg="white").grid(row=6, column=1, padx=20)
        tk.Label(self.tank_frame, text="", font=("Arial", 10), bg="white").grid(row=7, column=1, padx=20)
        tk.Label(self.tank_frame, text="", font=("Arial", 10), bg="white").grid(row=8, column=1, padx=20)
        self.tank_displays[tank1_bio['id']] = {
            'canvas': tank1_canvas, 'fill_bar': tank1_fill, 'height_var': tank1_height_var, 'height_window': tank1_height_window, 'level_var': tank1_level_var
        }
        logging.info("Added Tank 1 labels")

        # Tank 2 Label
        tank2_level_var = tk.StringVar(value=f"{int(tank2_fleet['level']):,} gal")
        tk.Label(self.tank_frame, textvariable=tank2_level_var, font=("Arial", 8), bg="white").grid(row=2, column=2, padx=20)
        tk.Label(self.tank_frame, text="", font=("Arial", 12), bg="white").grid(row=3, column=2, padx=20)
        tk.Label(self.tank_frame, text="", font=("Arial", 10), bg="white").grid(row=4, column=2, padx=20)
        tk.Label(self.tank_frame, text="", font=("Arial", 10), bg="white").grid(row=5, column=2, padx=20)
        tk.Label(self.tank_frame, text="", font=("Arial", 10), bg="white").grid(row=6, column=2, padx=20)
        tk.Label(self.tank_frame, text="", font=("Arial", 10), bg="white").grid(row=7, column=2, padx=20)
        tk.Label(self.tank_frame, text="", font=("Arial", 10), bg="white").grid(row=8, column=2, padx=20)
        self.tank_displays[tank2_fleet['id']] = {
            'canvas': tank2_canvas, 'fill_bar': tank2_fill, 'height_var': tank2_height_var, 'height_window': tank2_height_window, 'level_var': tank2_level_var
        }
        logging.info("Added Tank 2 labels")

        # Tank 3 Label
        tank3_level_var = tk.StringVar(value=f"{int(tank3_fleet['level']):,} gal")
        tk.Label(self.tank_frame, textvariable=tank3_level_var, font=("Arial", 8), bg="white").grid(row=2, column=3, padx=20)
        tk.Label(self.tank_frame, text="", font=("Arial", 12), bg="white").grid(row=3, column=3, padx=20)
        tk.Label(self.tank_frame, text="", font=("Arial", 10), bg="white").grid(row=4, column=3, padx=20)
        tk.Label(self.tank_frame, text="", font=("Arial", 10), bg="white").grid(row=5, column=3, padx=20)
        tk.Label(self.tank_frame, text="", font=("Arial", 10), bg="white").grid(row=6, column=3, padx=20)
        tk.Label(self.tank_frame, text="", font=("Arial", 10), bg="white").grid(row=7, column=3, padx=20)
        tk.Label(self.tank_frame, text="", font=("Arial", 10), bg="white").grid(row=8, column=3, padx=20)
        self.tank_displays[tank3_fleet['id']] = {
            'canvas': tank3_canvas, 'fill_bar': tank3_fill, 'height_var': tank3_height_var, 'height_window': tank3_height_window, 'level_var': tank3_level_var
        }
        logging.info("Added Tank 3 labels")

        # Tank 4 Label
        tank4_level_var = tk.StringVar(value=f"{int(tank4_bio['level']):,} gal")
        tk.Label(self.tank_frame, textvariable=tank4_level_var, font=("Arial", 8), bg="white").grid(row=2, column=4, padx=20)
        tk.Label(self.tank_frame, text="", font=("Arial", 12), bg="white").grid(row=3, column=4, padx=20)
        tk.Label(self.tank_frame, text="", font=("Arial", 10), bg="white").grid(row=4, column=4, padx=20)
        tk.Label(self.tank_frame, text="", font=("Arial", 10), bg="white").grid(row=5, column=4, padx=20)
        tk.Label(self.tank_frame, text="", font=("Arial", 10), bg="white").grid(row=6, column=4, padx=20)
        tk.Label(self.tank_frame, text="", font=("Arial", 10), bg="white").grid(row=7, column=4, padx=20)
        tk.Label(self.tank_frame, text="", font=("Arial", 10), bg="white").grid(row=8, column=4, padx=20)
        self.tank_displays[tank4_bio['id']] = {
            'canvas': tank4_canvas, 'fill_bar': tank4_fill, 'height_var': tank4_height_var, 'height_window': tank4_height_window, 'level_var': tank4_level_var
        }
        logging.info("Added Tank 4 labels")

        # Tank 5 Label
        tank5_level_var = tk.StringVar(value=f"{int(tank5_bio['level']):,} gal")
        tk.Label(self.tank_frame, textvariable=tank5_level_var, font=("Arial", 8), bg="white").grid(row=2, column=5, padx=20)
        tk.Label(self.tank_frame, text="", font=("Arial", 12), bg="white").grid(row=3, column=5, padx=20)
        tk.Label(self.tank_frame, text="", font=("Arial", 10), bg="white").grid(row=4, column=5, padx=20)
        tk.Label(self.tank_frame, text="", font=("Arial", 10), bg="white").grid(row=5, column=5, padx=20)
        tk.Label(self.tank_frame, text="", font=("Arial", 10), bg="white").grid(row=6, column=5, padx=20)
        tk.Label(self.tank_frame, text="", font=("Arial", 10), bg="white").grid(row=7, column=5, padx=20)
        tk.Label(self.tank_frame, text="", font=("Arial", 10), bg="white").grid(row=8, column=5, padx=20)
        self.tank_displays[tank5_bio['id']] = {
            'canvas': tank5_canvas, 'fill_bar': tank5_fill, 'height_var': tank5_height_var, 'height_window': tank5_height_window, 'level_var': tank5_level_var
        }
        logging.info("Added Tank 5 labels")

        # Load and place the logo for top right corner with resizing
        logo_filename = LOGO_FILE_PATH
        try:
            logo_path = resource_path(logo_filename)
            logging.info("Attempting to load logo from: %s", logo_path)
            if not os.path.exists(logo_path):
                logging.error("Logo file does not exist at: %s", logo_path)
                raise FileNotFoundError(f"Logo file not found at: {logo_path}")
            logo_image = Image.open(logo_path)
            # Resize logo if width exceeds 300 pixels, maintaining aspect ratio
            max_width = 300
            if logo_image.width > max_width:
                aspect_ratio = logo_image.height / logo_image.width
                new_width = max_width
                new_height = int(new_width * aspect_ratio)
                logo_image = logo_image.resize((new_width, new_height), Image.Resampling.LANCZOS)
            logo_photo = ImageTk.PhotoImage(logo_image)
            self.logo_right = tk.Label(root, image=logo_photo, bg="white")
            self.logo_right.image = logo_photo  # Keep a reference to avoid garbage collection
            logging.info("Logo loaded successfully, width: %d, height: %d", logo_image.width, logo_image.height)
        except Exception as e:
            logging.error("Error loading logo: %s", e)
            self.logo_right = tk.Label(root, text="Logo Missing", font=("Arial", 10), bg="white", fg="red")
            tk.messagebox.showerror("Error", f"Failed to load logo: {e}")

        # Position the logo in the top right corner with padding and bring to front
        self.logo_right.place(x=1300 - 10, y=10, anchor="ne")  # Top right with 10px padding
        self.logo_right.lift()  # Bring the logo to the front to avoid being covered
        logging.info("Logo placed at x=%d, y=%d with anchor='ne'", 1300 - 10, 10)

        # Add "Benchmark Fuel Terminal" label at the top center
        title_label = tk.Label(root, text="Benchmark Fuel Terminal", font=("Arial", 16, "bold"), bg="white")
        title_label.place(x=650, y=20, anchor="center")  # Top center with 20px padding from top
        title_label.lift()  # Bring the label to the front to avoid being covered
        logging.info("Title label 'Benchmark Fuel Terminal' placed at x=650, y=20 with anchor='center'")

        # Force a window update to ensure rendering
        self.root.update()
        logging.info("Updated root window")

        # Start Modbus polling thread if the client was initialized
        if self.client:
            try:
                self.modbus_thread = threading.Thread(target=self.modbus_polling, daemon=True)
                logging.info("Created Modbus polling thread")
                self.modbus_thread.start()
                logging.info("Started Modbus polling thread")
            except Exception as e:
                logging.error("Failed to start Modbus polling thread: %s", e)
        else:
            logging.warning("Modbus client not initialized; skipping polling thread")

    def setup_database(self):
        """Set up the SQLite database and table in the main thread."""
        try:
            logging.info("Connecting to SQLite database at: %s", self.db_path)
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                logging.info("Creating tank_readings table if it doesn't exist")
                # Create table if it doesn't exist
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS tank_readings (
                        timestamp TEXT,
                        big_tank_gallons FLOAT,
                        tank1_gallons FLOAT,
                        tank2_gallons FLOAT
                    )
                ''')
                logging.info("Table creation executed")

                # Enable WAL mode for better concurrency
                cursor.execute("PRAGMA journal_mode=WAL")
                logging.info("Enabled WAL mode")

                # Set a timeout for busy handling
                cursor.execute("PRAGMA busy_timeout=5000")  # 5 seconds
                logging.info("Set busy timeout to 5000ms")

                # Verify that the table was created
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tank_readings'")
                if cursor.fetchone():
                    logging.info("Confirmed tank_readings table exists")
                else:
                    logging.error("tank_readings table does not exist after creation attempt")

                conn.commit()
                logging.info("Successfully set up SQLite database at: %s", self.db_path)
        except sqlite3.Error as e:
            logging.error("Failed to set up SQLite database: %s", e)
            raise  # Re-raise the exception to halt execution and debug

    def update_time_date(self):
        """Update the time and date label every second."""
        if not self.running:
            return
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.time_date_var.set(current_time)
        self.time_date_after_id = self.root.after(1000, self.update_time_date)  # Schedule the next update in 1000ms (1 second)

    def add_scale(self, canvas, tank_height, canvas_height, canvas_width):
        """Draw a height scale on the tank canvas."""
        pixels_per_foot = canvas_height / tank_height
        for ft in range(0, int(tank_height) + 1):
            y = canvas_height - (ft * pixels_per_foot)
            y_adjusted = max(5, min(canvas_height - 5, y))
            if ft % 5 == 0:
                canvas.create_line(0, y_adjusted, 10, y_adjusted, width=2, fill="black")
                canvas.create_text(12, y_adjusted, text=f"{ft}", font=("Arial", 8, "bold"), anchor="w")
            else:
                canvas.create_line(0, y_adjusted, 5, y_adjusted, width=1, fill="black")

    def ping_device(self, ip_address):
        """Ping the device to check if it's reachable."""
        try:
            param = '-n' if platform.system().lower() == 'windows' else '-c'
            command = ['ping', param, '1', ip_address]
            logging.info("Executing ping command: %s", command)
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
            if result.returncode == 0:
                logging.info("Ping to %s successful: %s", ip_address, result.stdout.decode())
                return True
            else:
                logging.warning("Ping to %s failed: %s", ip_address, result.stderr.decode())
                return False
        except subprocess.TimeoutExpired:
            logging.error("Ping to %s timed out", ip_address)
            return False
        except Exception as e:
            logging.error("Ping to %s failed: %s", ip_address, e)
            return False

    def modbus_polling(self):
        """Poll Modbus data and update GUI."""
        logging.info("Modbus polling thread started")
        last_poll_time = time.time()
        while self.running:
            try:
                start_time = time.time()
                elapsed = start_time - last_poll_time
                if elapsed < MIN_POLL_INTERVAL:
                    logging.warning("Polling too fast (%.3fs < %.3fs), slowing down", elapsed, MIN_POLL_INTERVAL)
                    time.sleep(MIN_POLL_INTERVAL - elapsed)

                # Log before pinging
                logging.info("Attempting to ping device at %s", self.adam_ip)
                if not self.ping_device(self.adam_ip):
                    logging.warning("Device %s unreachable, skipping fetch", self.adam_ip)
                    time.sleep(self.poll_interval)
                    continue
                logging.info("Device %s is reachable", self.adam_ip)

                # Log before fetching tank levels
                logging.info("Fetching tank levels from Modbus device")
                self.fetch_tank_levels()
                logging.info("Finished fetching tank levels")

                # Update GUI
                if self.running:
                    logging.info("Scheduling GUI update")
                    self.root.after(0, self.update_gui)
                    logging.info("GUI update scheduled")

                poll_duration = time.time() - start_time
                if poll_duration > self.poll_interval:
                    logging.warning("Polling took %.3fs, longer than interval %.3fs", poll_duration, self.poll_interval)

                last_poll_time = time.time()
                logging.info("Sleeping for poll interval: %.2fs", self.poll_interval)
                time.sleep(self.poll_interval)

            except Exception as e:
                logging.error("Polling loop error: %s", e)
                time.sleep(self.poll_interval)

    def fetch_tank_levels(self):
        """Fetch tank levels from Modbus device and log to database."""
        try:
            logging.info("Checking if Modbus client socket is open")
            if not self.client.is_socket_open():
                logging.info("Socket not open, attempting to connect to %s", self.adam_ip)
                if not self.client.connect():
                    logging.error("Failed to connect to %s", self.adam_ip)
                    return
                logging.info("Successfully connected to %s", self.adam_ip)
            else:
                logging.info("Modbus client socket already open")

            self.client.unit_id = self.unit_id
            logging.info("Reading holding registers 0-2 with unit_id=%d", self.unit_id)
            tags = self.client.read_holding_registers(0, 3)
            if tags.isError():
                logging.error("Modbus read error: %s", tags)
                return
            # Log raw register values
            logging.info("Modbus registers: %s", tags.registers)
            level1 = (float(tags.registers[0]) / 65535) * CAPACITIES['Big Tank']['gallons']
            level2 = (float(tags.registers[1]) / 65535) * CAPACITIES['Tank 1 - Bio']['gallons']
            level3 = (float(tags.registers[2]) / 65535) * CAPACITIES['Tank 2 - Fleet']['gallons']
            self.tank_levels[0]['level'] = round(level1, 2)
            self.tank_levels[1]['level'] = round(level2, 2)
            self.tank_levels[2]['level'] = round(level3, 2)
            self.tank_levels[3]['level'] = round(level3, 2)  # Mirrored from Tank 2
            self.tank_levels[4]['level'] = round(level2, 2)  # Mirrored from Tank 1
            self.tank_levels[5]['level'] = round(level2, 2)  # Mirrored from Tank 1
            self.last_update = datetime.now()
            logging.info("Successfully connected to ADAM-6017")
            logging.info("Tank levels updated: Big Tank=%.2f, Tank 1=%.2f, Tank 2=%.2f", level1, level2, level3)

            # Log to SQLite database using a new connection in this thread
            try:
                logging.info("Logging tank levels to SQLite database")
                with sqlite3.connect(self.db_path) as conn:
                    cursor = conn.cursor()
                    # Set a timeout for busy handling
                    cursor.execute("PRAGMA busy_timeout=5000")  # 5 seconds
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    cursor.execute('''
                        INSERT INTO tank_readings (timestamp, big_tank_gallons, tank1_gallons, tank2_gallons)
                        VALUES (?, ?, ?, ?)
                    ''', (timestamp, level1, level2, level3))
                    conn.commit()
                    logging.info("Logged tank levels to database: timestamp=%s, Big Tank=%.2f, Tank 1=%.2f, Tank 2=%.2f", timestamp, level1, level2, level3)
            except sqlite3.Error as e:
                logging.error("Failed to log tank levels to database: %s", e)

        except (ConnectionException, ModbusException) as e:
            logging.error("Modbus error: %s", e)
        except Exception as e:
            logging.error("Unexpected error in fetch_tank_levels: %s", e)

    def update_gui(self):
        """Update the GUI with the latest tank levels."""
        logging.info("Updating GUI with tank levels")
        fill_colors = {
            'Big Tank': "#32CD32",
            'Tank 1 - Bio': "#FFA500",
            'Tank 2 - Fleet': "#FFFF00",
            'Tank 3 - Fleet': "#FFFF00",
            'Tank 4 - Bio': "#FFA500",
            'Tank 5 - Bio': "#FFA500"
        }
        if self.last_update and (datetime.now() - self.last_update) > self.stale_threshold:
            logging.warning("Data may be stale")
            for tank in self.tank_levels:
                self.tank_displays[tank['id']]['canvas'].itemconfig(self.tank_displays[tank['id']]['fill_bar'], fill="gray")
        else:
            for tank in self.tank_levels:
                self.tank_displays[tank['id']]['canvas'].itemconfig(self.tank_displays[tank['id']]['fill_bar'], fill=fill_colors[tank['id']])

        for tank in self.tank_levels:
            if tank['id'] == 'Big Tank':
                # Update Total Gallons and Usable Gallons
                self.tank_displays[tank['id']]['total_gallons_var'].set(f"Total Gallons: {int(tank['level']):,}")
                self.tank_displays[tank['id']]['usable_gallons_var'].set(f"Usable Gallons: {max(0, int(tank['level'] - BIG_TANK_HEEL)):,}")
                
                # Calculate Barrels in Tank (Total Gallons / 42)
                barrels_in_tank = int(tank['level']) // GALLONS_PER_BBL
                self.tank_displays[tank['id']]['barrels_in_tank_var'].set(f"{barrels_in_tank:,}")
                
                # Calculate ROOM SHEET in BBLs (Usable Capacity - Barrels in Tank)
                usable_capacity_bbls = 12495  # Static value as per request
                room_sheet_bbls = max(0, usable_capacity_bbls - barrels_in_tank)
                self.tank_displays[tank['id']]['room_sheet_var'].set(f"ROOM SHEET in BBLs = {room_sheet_bbls:,}")
            else:
                self.tank_displays[tank['id']]['level_var'].set(f"{int(tank['level']):,} gal")
            percent_full = tank['level'] / tank['capacity']
            total_feet = percent_full * tank['height']
            feet = int(total_feet)
            inches = int((total_feet - feet) * 12)
            self.tank_displays[tank['id']]['height_var'].set(f"{feet} ft {inches} in")
            percent_full = min(max(percent_full * 100, 0), 100)
            canvas = self.tank_displays[tank['id']]['canvas']
            fill_bar = self.tank_displays[tank['id']]['fill_bar']
            height_window = self.tank_displays[tank['id']]['height_window']
            canvas_height = canvas.winfo_height()
            fill_height = canvas_height - (percent_full / 100 * canvas_height)
            canvas.coords(fill_bar, 0, fill_height, canvas.winfo_width(), canvas_height)
            x_center = canvas.winfo_width() / 2
            y_pos = max(10, fill_height - 10)
            canvas.coords(height_window, x_center, y_pos)
        logging.info("GUI update completed")

    def cleanup(self):
        """Clean up resources on application close after verifying exit code."""
        # Skip cleanup if already performed
        if self.cleaned_up:
            return  # Prevent multiple cleanup calls

        # Check if the Tkinter root window is still active
        try:
            self.root.winfo_exists()  # This will raise a TclError if the window is destroyed
            # Prompt for exit code
            exit_code = tkinter.simpledialog.askstring(
                "Exit Confirmation",
                "Enter the exit code to close the program (e.g., 12345):",
                parent=self.root
            )

            # Check if the exit code is correct
            if exit_code != "12345":
                if exit_code is None:  # User clicked Cancel
                    logging.info("Exit canceled by user")
                else:
                    logging.warning("Incorrect exit code entered: %s", exit_code)
                    tkinter.messagebox.showerror("Error", "Incorrect exit code. Program will not close.")
                return  # Do not proceed with cleanup

            logging.info("Correct exit code entered, proceeding with cleanup")
        except tk.TclError:
            # If the window is already destroyed (e.g., called via atexit), proceed with cleanup without prompting
            logging.info("Tkinter root window already destroyed, proceeding with cleanup without prompting")

        # Proceed with cleanup
        self.cleaned_up = True
        self.running = False

        # Cancel any scheduled after calls
        if self.time_date_after_id is not None:
            try:
                self.root.after_cancel(self.time_date_after_id)
                logging.info("Cancelled update_time_date after call")
            except tk.TclError as e:
                logging.warning("Failed to cancel update_time_date after call: %s", e)

        # Close Modbus client
        if self.client and self.client.is_socket_open():
            self.client.close()
            logging.info("Closed Modbus client connection")

        # Destroy the root window if not already destroyed
        try:
            self.root.destroy()
            logging.info("Destroyed Tkinter root window")
        except tk.TclError as e:
            logging.warning("Root window already destroyed: %s", e)

if __name__ == "__main__":
    try:
        root = tk.Tk()
        app = TankLevelGUI(root)
        root.protocol("WM_DELETE_WINDOW", app.cleanup)
        root.mainloop()
    except Exception as e:
        logging.error("Failed to start application: %s", e, exc_info=True)
        raise  # Re-raise the exception to see it in the console