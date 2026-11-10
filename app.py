import os
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["PYTHONUNBUFFERED"] = "1"
import re   
import cv2
import traceback
import time
import threading   
import hashlib    
from functools import wraps
from datetime import datetime  
import csv      
import io 
from flask import Flask, render_template, request, redirect, url_for, Response, jsonify, session, flash, make_response
from werkzeug.security import generate_password_hash, check_password_hash
from detector import TrafficDetector
from traffic_logic import TrafficLogic     
from violation_detector import ViolationDetector   
import mysql.connector      
                                                        
# Load environment variables (.env)
def _load_env_fallback():
    env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if os.path.exists(env_file):
        try:
            with open(env_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    k, v = line.split('=', 1)
                    k, v = k.strip(), v.strip().strip('"\'')
                    if k and k not in os.environ:
                        os.environ[k] = v
        except Exception:
            pass

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    _load_env_fallback()

# --- Configuration ---   
UPLOAD_FOLDER = 'uploads'           
ALLOWED_EXTENSIONS = {'mp4', 'avi', 'mov', 'mkv'}

# MySQL Configuration (loaded strictly from .env)
DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "traffic_db")

# Default account credentials (loaded strictly from .env, zero secrets in code)
DEFAULT_ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
DEFAULT_ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")

USERNAME_RE = re.compile(r'^[A-Za-z0-9_.-]{3,32}$')
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['SECRET_KEY'] = os.getenv("SECRET_KEY") or os.urandom(24).hex()      

# --- Database Setup ---
def get_db():
    conn = mysql.connector.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME, connection_timeout=5, use_pure=True
    )
    return conn

def setup_database():
    """Creates the database and user table if they don't exist, and seeds the default admin account."""
    # First connect without DB to create it if it doesn't exist
    temp_conn = mysql.connector.connect(host=DB_HOST, user=DB_USER, password=DB_PASSWORD, connection_timeout=5, use_pure=True)
    temp_cursor = temp_conn.cursor()
    temp_cursor.execute(f"CREATE DATABASE IF NOT EXISTS {DB_NAME}")
    temp_conn.close()

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTO_INCREMENT,
            username VARCHAR(255) NOT NULL UNIQUE,
            email VARCHAR(255) UNIQUE,
            password_hash VARCHAR(255) NOT NULL,
            role VARCHAR(50) NOT NULL DEFAULT 'operator',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN email VARCHAR(255) UNIQUE")
    except mysql.connector.Error:
        pass # Column already exists

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS traffic_logs (
            id INTEGER PRIMARY KEY AUTO_INCREMENT,
            lane_id INTEGER NOT NULL,
            green_duration INTEGER NOT NULL,
            cars INTEGER DEFAULT 0,
            buses INTEGER DEFAULT 0,
            trucks INTEGER DEFAULT 0,
            motorcycles INTEGER DEFAULT 0,
            ambulances INTEGER DEFAULT 0,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS challans (
            id INTEGER PRIMARY KEY AUTO_INCREMENT,
            lane_id INTEGER NOT NULL,
            vehicle_type VARCHAR(50) DEFAULT 'Vehicle',
            plate_number VARCHAR(100) NOT NULL,
            violation_type VARCHAR(100) DEFAULT 'Red Light Violation',
            fine_amount INTEGER DEFAULT 1000,
            evidence_image LONGTEXT NOT NULL,
            status VARCHAR(50) DEFAULT 'Unpaid',
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    try:
        cursor.execute("ALTER TABLE challans MODIFY COLUMN evidence_image TEXT NOT NULL")
    except Exception:
        pass

    if DEFAULT_ADMIN_USERNAME and DEFAULT_ADMIN_PASSWORD:
        cursor.execute("SELECT * FROM users WHERE username = %s", (DEFAULT_ADMIN_USERNAME,))
        if cursor.fetchone() is None:
            hashed_password = generate_password_hash(DEFAULT_ADMIN_PASSWORD)
            cursor.execute(
                "INSERT INTO users (username, email, password_hash, role) VALUES (%s, %s, %s, %s)",
                (DEFAULT_ADMIN_USERNAME, None, hashed_password, 'admin')
            )
            print("Admin user created in MySQL database.")

    conn.commit()
    conn.close()

def verify_password(user_row, plain_password):
    stored_hash = user_row['password_hash']

    if check_password_hash(stored_hash, plain_password):
        return True

    if len(stored_hash) == 64 and hashlib.sha256(plain_password.encode()).hexdigest() == stored_hash:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET password_hash = %s WHERE id = %s",
            (generate_password_hash(plain_password), user_row['id'])
        )
        conn.commit()
        conn.close()
        return True

    return False

# --- Auth decorators ---
def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return view(*args, **kwargs)
    return wrapped

def roles_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if 'user_id' not in session:
                return redirect(url_for('login'))
            if session.get('role') not in roles:
                flash('You do not have permission to view that page.', 'danger')
                return redirect(url_for('home'))
            return view(*args, **kwargs)
        return wrapped
    return decorator

# --- Global Variables & Initializations ---
def init_default_video_paths():
    # Feeds start in clean standby mode; videos are NOT automatically played without user action
    return {1: None, 2: None, 3: None, 4: None}

video_paths = init_default_video_paths()
video_caps = {1: None, 2: None, 3: None, 4: None}
capture_locks = {1: threading.Lock(), 2: threading.Lock(), 3: threading.Lock(), 4: threading.Lock()}

try:
    detector = TrafficDetector(vehicle_model_path='yolov8n.pt', ambulance_model_path='best.pt')
except Exception as e:
    print(f"Error loading YOLO models: {e}")
    detector = None

try:
    violation_detector = ViolationDetector(plate_model_path='license_plate.pt')
except Exception as e:
    print(f"Error loading ViolationDetector: {e}")
    violation_detector = None

traffic_manager = TrafficLogic()

# --- Helper Functions ---
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def draw_ui_elements(frame, lane_id, density, ambulance, status):
    cv2.rectangle(frame, (10, 10), (70, 170), (50, 50, 50), -1)
    cv2.rectangle(frame, (10, 10), (70, 170), (255, 255, 255), 1)
    red_color = (0, 0, 255) if status == 'red' else (40, 40, 40)
    orange_color = (0, 165, 255) if status == 'orange' else (40, 40, 40)
    green_color = (0, 255, 0) if status == 'green' else (40, 40, 40)
    cv2.circle(frame, (40, 40), 20, red_color, -1)
    cv2.circle(frame, (40, 90), 20, orange_color, -1)
    cv2.circle(frame, (40, 140), 20, green_color, -1)
    cv2.putText(frame, f"Lane: {lane_id}", (10, 200), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
    cv2.putText(frame, f"Density: {density}", (10, 230), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
    if ambulance:
        cv2.putText(frame, "AMBULANCE!", (10, 260), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2, cv2.LINE_AA)
    return frame

# --- Video Streaming Generator ---
def generate_frames(lane_id):
    global video_caps
    import time
    import numpy as np

    while True:
        video_path = video_paths.get(lane_id)
        
        if not video_path:
            traffic_manager.update_lane_data(lane_id, 0, False, {})
            blank_image = np.zeros((360, 640, 3), dtype=np.uint8)
            blank_image[:] = (20, 15, 10)  # Dark cyber navy
            
            # Tactical corner brackets
            c = (200, 180, 50)
            sz = 18
            t = 2
            cv2.line(blank_image, (20, 20), (20 + sz, 20), c, t)
            cv2.line(blank_image, (20, 20), (20, 20 + sz), c, t)
            cv2.line(blank_image, (620, 20), (620 - sz, 20), c, t)
            cv2.line(blank_image, (620, 20), (620, 20 + sz), c, t)
            cv2.line(blank_image, (20, 340), (20 + sz, 340), c, t)
            cv2.line(blank_image, (20, 340), (20, 340 - sz), c, t)
            cv2.line(blank_image, (620, 340), (620 - sz, 340), c, t)
            cv2.line(blank_image, (620, 340), (620, 340 - sz), c, t)
            
            cv2.circle(blank_image, (170, 142), 6, (50, 180, 245), -1)
            cv2.putText(blank_image, f"SECTOR {lane_id} // STANDBY", (190, 150), cv2.FONT_HERSHEY_DUPLEX, 0.75, (255, 255, 255), 2, cv2.LINE_AA)
            cv2.putText(blank_image, "NO ACTIVE VIDEO STREAM DETECTED", (170, 190), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (160, 160, 160), 1, cv2.LINE_AA)
            cv2.putText(blank_image, "Click 'Load Demo Feeds' or 'Cameras' to begin", (145, 225), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (180, 220, 50), 1, cv2.LINE_AA)
            
            ret, encodedImage = cv2.imencode('.jpg', blank_image)
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + bytearray(encodedImage) + b'\r\n')
            time.sleep(1)
            continue

        frame = None
        with capture_locks[lane_id]:
            if video_caps.get(lane_id) is None:
                try:
                    cap = cv2.VideoCapture(video_path)
                    if str(video_path).startswith('http'):
                        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    video_caps[lane_id] = cap
                except Exception:
                    pass
                    
            cap = video_caps.get(lane_id)
            if cap is None or not cap.isOpened():
                time.sleep(0.5)
                continue

            try:
                ret, raw_frame = cap.read()
                if not ret or raw_frame is None or getattr(raw_frame, 'size', 0) == 0:
                    if str(video_path).startswith('http'):
                        try:
                            cap.release()
                        except Exception:
                            pass
                        video_caps[lane_id] = None
                        time.sleep(0.5)
                        continue
                    else:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        ret, raw_frame = cap.read()
                        if not ret or raw_frame is None or getattr(raw_frame, 'size', 0) == 0:
                            time.sleep(0.1)
                            continue

                frame = raw_frame.copy()
            except Exception:
                try:
                    cap.release()
                except Exception:
                    pass
                video_caps[lane_id] = None
                time.sleep(0.5)
                continue

        if frame is None:
            time.sleep(0.05)
            continue

        try:
            h, w = frame.shape[:2]
            if h <= 0 or w <= 0:
                time.sleep(0.05)
                continue

            # Resize frame for real-time responsiveness and CPU optimization
            if w > 640:
                scale = 640.0 / w
                new_h = max(1, int(h * scale))
                frame = cv2.resize(frame, (640, new_h))

            if detector:
                processed_frame, ambulance_detected, detailed_counts = detector.process_frame(frame, lane_id=lane_id)
                density = sum(detailed_counts.values())
            else:
                processed_frame, density, ambulance_detected, detailed_counts = frame, 0, False, {}

            traffic_manager.update_lane_data(lane_id, density, ambulance_detected, detailed_counts)
            current_state = traffic_manager.get_system_state()
            lane_status = current_state[lane_id]['status']
            final_frame = draw_ui_elements(processed_frame, lane_id, density, ambulance_detected, lane_status)

            if violation_detector:
                final_frame = violation_detector.draw_stop_line(final_frame, lane_status)
                vehicle_boxes = detector.get_last_vehicle_boxes(lane_id=lane_id) if detector else []
                final_frame = violation_detector.check_violation(
                    frame=final_frame,
                    lane_id=lane_id,
                    vehicle_boxes=vehicle_boxes,
                    lane_status=lane_status,
                    db_conn_func=get_db
                )

            (flag, encodedImage) = cv2.imencode(".jpg", final_frame)
            if not flag:
                continue

            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + bytearray(encodedImage) + b'\r\n')
        except Exception:
            traceback.print_exc()
            time.sleep(0.2)

# --- AUTH ROUTES ---
@app.route('/register', methods=['GET', 'POST'])
def register():
    if 'user_id' in session:
        return redirect(url_for('home'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip() or None
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')

        error = None
        if not USERNAME_RE.match(username):
            error = 'Username must be 3-32 characters (letters, numbers, . _ - only).'
        elif email and not EMAIL_RE.match(email):
            error = 'Please enter a valid email address.'
        elif len(password) < 8:
            error = 'Password must be at least 8 characters long.'
        elif password != confirm_password:
            error = 'Passwords do not match.'

        if error:
            flash(error, 'danger')
            return render_template('register.html', username=username, email=email or '')

        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT id FROM users WHERE username = %s", (username,))
        if cursor.fetchone() is not None:
            conn.close()
            flash('That username is already taken.', 'danger')
            return render_template('register.html', username=username, email=email or '')

        if email:
            cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
            if cursor.fetchone() is not None:
                conn.close()
                flash('That email is already registered.', 'danger')
                return render_template('register.html', username=username, email=email or '')

        password_hash = generate_password_hash(password)
        cursor.execute(
            "INSERT INTO users (username, email, password_hash, role) VALUES (%s, %s, %s, %s)",
            (username, email, password_hash, 'operator')
        )
        conn.commit()
        conn.close()

        flash('Account created successfully. Please log in.', 'success')
        return redirect(url_for('login'))

    return render_template('register.html', username='', email='')


@app.route('/', methods=['GET', 'POST'])
def login():
    if 'user_id' in session:
        return redirect(url_for('home'))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        conn = get_db()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM users WHERE username = %s", (username,))
        user = cursor.fetchone()
        conn.close()

        if user and verify_password(user, password):
            session.clear()
            session['user_id'] = user['id']
            session['logged_in'] = True 
            session['username'] = user['username']
            session['role'] = user['role']
            flash('Login successful!', 'success')
            return redirect(url_for('home'))
        else:
            flash('Invalid credentials. Please try again.', 'danger')

    return render_template('login.html')

@app.route('/home')
@login_required
def home():
    return render_template('project_home.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))

@app.route('/operators')
@roles_required('admin')
def manage_operators():
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT id, username, email, role, created_at FROM users ORDER BY created_at DESC")
    users = cursor.fetchall()
    conn.close()
    return render_template('operators.html', users=users)

@app.route('/upload', methods=['GET', 'POST'])
@login_required
def upload_page():
    global video_paths, video_caps
    if request.method == 'POST':
        for i in range(1, 5):
            file_key = f'video{i}'
            if file_key not in request.files:
                continue
            file = request.files[file_key]
            if file.filename == '' or not allowed_file(file.filename):
                continue
            ext = file.filename.rsplit('.', 1)[1].lower()
            unique_filename = f'lane_{i}_{int(time.time())}.{ext}'
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
            file.save(save_path)
            with capture_locks[i]:
                if video_caps.get(i) is not None:
                    try:
                        video_caps[i].release()
                    except Exception:
                        pass
                    video_caps[i] = None
                video_paths[i] = save_path
        return redirect(url_for('dashboard'))
    return render_template('upload.html')

@app.route('/set_ip_stream', methods=['POST'])
@login_required
def set_ip_stream():
    global video_paths, video_caps
    camera_ip = request.form.get('camera_ip', '').strip()
    stream_lane = request.form.get('stream_lane', 'ALL').strip()

    if not camera_ip:
        flash("Please enter a valid IP address.", "danger")
        return redirect(url_for('upload_page'))

    # Format URL properly for IP Webcam
    if not camera_ip.startswith('http://') and not camera_ip.startswith('https://'):
        if ':' not in camera_ip:
            stream_url = f"http://{camera_ip}:8080/video"
        else:
            stream_url = f"http://{camera_ip}/video"
    else:
        stream_url = camera_ip
        if not stream_url.endswith('/video'):
            stream_url = stream_url.rstrip('/') + '/video'

    if stream_lane == 'ALL':
        for i in range(1, 5):
            with capture_locks[i]:
                if video_caps.get(i) is not None:
                    try:
                        video_caps[i].release()
                    except Exception:
                        pass
                    video_caps[i] = None
                video_paths[i] = stream_url
        flash(f"Live stream connected for ALL lanes from {camera_ip}!", "success")
    elif stream_lane in ['1', '2', '3', '4']:
        idx = int(stream_lane)
        with capture_locks[idx]:
            if video_caps.get(idx) is not None:
                try:
                    video_caps[idx].release()
                except Exception:
                    pass
                video_caps[idx] = None
            video_paths[idx] = stream_url
        flash(f"Live stream connected for Lane {stream_lane} from {camera_ip}!", "success")
    else:
        flash("Invalid lane selected.", "danger")
        return redirect(url_for('upload_page'))

    return redirect(url_for('dashboard'))


@app.route('/load_demo_videos')
@login_required
def load_demo_videos():
    global video_paths, video_caps
    loaded = 0
    for i in range(1, 5):
        sample_path = f"uploads/lane_{i}.mp4"
        if os.path.exists(sample_path):
            with capture_locks[i]:
                if video_caps.get(i) is not None:
                    try:
                        video_caps[i].release()
                    except Exception:
                        pass
                    video_caps[i] = None
                video_paths[i] = sample_path
                loaded += 1
    if loaded > 0:
        flash(f"Loaded simulation feeds across {loaded} sectors!", "success")
    else:
        flash("No sample video files found in uploads/ folder.", "warning")
    return redirect(url_for('dashboard'))

@app.route('/clear_feeds')
@login_required
def clear_feeds():
    global video_paths, video_caps
    for i in range(1, 5):
        with capture_locks[i]:
            if video_caps.get(i) is not None:
                try:
                    video_caps[i].release()
                except Exception:
                    pass
                video_caps[i] = None
            video_paths[i] = None
        traffic_manager.update_lane_data(i, 0, False, {})
    flash("All video feeds cleared. System returned to standby mode.", "info")
    return redirect(url_for('dashboard'))

@app.route('/dashboard')
@login_required
def dashboard():
    return render_template('dashboard.html')

@app.route('/analysis')  
@login_required
def analysis_page():
    return render_template('analysis.html')

@app.route('/video_feed/<int:lane_id>')
@login_required
def video_feed(lane_id):
    if lane_id not in [1, 2, 3, 4]:
        return "Invalid Lane ID", 404
    return Response(generate_frames(lane_id), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/status_api')
def status_api():
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify(traffic_manager.get_system_state())

@app.route('/api/analysis_data')
def analysis_data():
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify(traffic_manager.get_analysis_data())

@app.route('/api/generate_report')
@login_required
def generate_report():
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM traffic_logs ORDER BY timestamp DESC")
    logs = cursor.fetchall()
    conn.close()

    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['ID', 'Lane ID', 'Green Duration (s)', 'Cars', 'Buses', 'Trucks', 'Motorcycles', 'Ambulances', 'Timestamp'])
    
    for row in logs:
        cw.writerow([row['id'], row['lane_id'], row['green_duration'], row['cars'], row['buses'], row['trucks'], row['motorcycles'], row['ambulances'], row['timestamp']])

    output = make_response(si.getvalue())
    output.headers["Content-Disposition"] = "attachment; filename=traffic_analysis_report.csv"
    output.headers["Content-type"] = "text/csv"
    return output

@app.route('/api/generate_pdf_report')
@login_required
def generate_pdf_report():
    from fpdf import FPDF
    
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM traffic_logs ORDER BY timestamp DESC LIMIT 50")
    logs = cursor.fetchall()
    conn.close()

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("helvetica", 'B', 16)
    pdf.cell(200, 10, txt="Traffic Analysis Report", ln=1, align='C')
    
    pdf.set_font("helvetica", size=10)
    pdf.ln(10)
    
    # Table Header
    headers = ['ID', 'Lane', 'Green(s)', 'Cars', 'Buses', 'Trucks', 'Motorcycles', 'Ambulances']
    col_widths = [15, 15, 20, 15, 15, 15, 25, 25]
    
    for i, header in enumerate(headers):
        pdf.cell(col_widths[i], 10, txt=header, border=1)
    pdf.ln()
    
    # Table Data
    for row in logs:
        pdf.cell(col_widths[0], 10, txt=str(row['id']), border=1)
        pdf.cell(col_widths[1], 10, txt=str(row['lane_id']), border=1)
        pdf.cell(col_widths[2], 10, txt=str(row['green_duration']), border=1)
        pdf.cell(col_widths[3], 10, txt=str(row['cars']), border=1)
        pdf.cell(col_widths[4], 10, txt=str(row['buses']), border=1)
        pdf.cell(col_widths[5], 10, txt=str(row['trucks']), border=1)
        pdf.cell(col_widths[6], 10, txt=str(row['motorcycles']), border=1)
        pdf.cell(col_widths[7], 10, txt=str(row['ambulances']), border=1)
        pdf.ln()

    out = pdf.output(dest='S')
    if isinstance(out, str):
        pdf_bytes = out.encode('latin1')
    else:
        pdf_bytes = bytes(out)
        
    response = make_response(pdf_bytes)
    response.headers['Content-Type'] = 'application/pdf'
    response.headers['Content-Disposition'] = 'attachment; filename=traffic_analysis_report.pdf'
    return response

@app.route('/api/auto_upload', methods=['POST'])
def auto_upload():
    global video_paths, video_caps
    if 'video' not in request.files:
        return jsonify({"error": "No video file provided"}), 400
    
    file = request.files['video']
    lane_id = request.form.get('lane_id', '')
    
    if file.filename == '':
        return jsonify({"error": "Empty filename"}), 400
        
    filename = f"auto_{lane_id}_{int(datetime.now().timestamp())}.mp4"
    save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(save_path)     
    
    try:
        for old_file in os.listdir(app.config['UPLOAD_FOLDER']):
            if old_file.startswith(f"auto_{lane_id}_") and old_file != filename:
                try:
                    os.remove(os.path.join(app.config['UPLOAD_FOLDER'], old_file))
                except Exception:
                    pass
    except Exception:
        pass

    if lane_id.upper() == 'ALL':
        for i in range(1, 5):
            with capture_locks[i]:
                if video_caps.get(i) is not None:
                    try:
                        video_caps[i].release()
                    except Exception:
                        pass
                    video_caps[i] = None
                video_paths[i] = save_path
    elif lane_id in ['1', '2', '3', '4']:
        idx = int(lane_id)
        with capture_locks[idx]:
            if video_caps.get(idx) is not None:
                try:
                    video_caps[idx].release()
                except Exception:
                    pass
                video_caps[idx] = None
            video_paths[idx] = save_path
    else:
        return jsonify({"error": "Invalid lane_id"}), 400
        
    return jsonify({"success": True, "message": f"Updated lane {lane_id}"})

# --- E-CHALLAN & VIOLATION ROUTES ---
@app.route('/challans')
@login_required
def challans():
    filter_status = request.args.get('status', 'all')
    conn = get_db()
    cursor = conn.cursor(dictionary=True)

    if filter_status in ['Paid', 'Unpaid']:
        cursor.execute("SELECT * FROM challans WHERE status = %s ORDER BY timestamp DESC", (filter_status,))
    else:
        cursor.execute("SELECT * FROM challans ORDER BY timestamp DESC")

    all_challans = cursor.fetchall()

    cursor.execute("SELECT COUNT(*) as total, SUM(CASE WHEN status='Unpaid' THEN 1 ELSE 0 END) as unpaid, SUM(CASE WHEN status='Paid' THEN 1 ELSE 0 END) as paid, SUM(fine_amount) as total_fine, SUM(CASE WHEN status='Unpaid' THEN fine_amount ELSE 0 END) as pending_fine, SUM(CASE WHEN status='Paid' THEN fine_amount ELSE 0 END) as collected_fine FROM challans")
    stats = cursor.fetchone()
    conn.close()

    return render_template('challans.html', 
                           challans=all_challans, 
                           stats=stats, 
                           current_filter=filter_status,
                           username=session.get('username'),
                           role=session.get('role'))

@app.route('/challans/<int:challan_id>/status', methods=['POST'])
@login_required
def update_challan_status(challan_id):
    new_status = request.form.get('status', 'Paid')
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE challans SET status = %s WHERE id = %s", (new_status, challan_id))
    conn.commit()
    conn.close()
    flash(f"Challan #{challan_id} marked as {new_status}.", 'success')
    return redirect(url_for('challans'))

@app.route('/challans/<int:challan_id>/print')
@login_required
def print_challan(challan_id):
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM challans WHERE id = %s", (challan_id,))
    challan = cursor.fetchone()
    conn.close()

    if not challan:
        flash("Challan not found.", "danger")
        return redirect(url_for('challans'))

    return render_template('challan_print.html', challan=challan)

@app.route('/api/recent_violations')
def api_recent_violations():
    if violation_detector:
        return jsonify({"violations": violation_detector.recent_alerts})
    return jsonify({"violations": []})

@app.route('/api/force_lane', methods=['POST'])
def api_force_lane():
    if 'user_id' not in session:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json() or {}
    try:
        lane_id = int(data.get('lane_id', 1))
        if 1 <= lane_id <= 4:
            traffic_manager.force_lane_green(lane_id)
            return jsonify({"status": "success", "lane": lane_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"error": "Invalid lane"}), 400



@app.route('/api/trigger_violation', methods=['POST'])
def api_trigger_violation():
    data = request.get_json() or {}
    try:
        lane_id = int(data.get('lane_id', 1))
    except (ValueError, TypeError):
        lane_id = 1
    
    frame = None
    if 1 <= lane_id <= 4:
        with capture_locks[lane_id]:
            cap = video_caps.get(lane_id)
            if cap and cap.isOpened():
                try:
                    ret, f = cap.read()
                    if ret and f is not None and getattr(f, 'size', 0) > 0:
                        frame = f.copy()
                except Exception:
                    pass

    if frame is None:
        video_path = video_paths.get(lane_id) or f"uploads/lane_{lane_id}.mp4"
        if os.path.exists(video_path):
            try:
                temp_cap = cv2.VideoCapture(video_path)
                ret, f = temp_cap.read()
                if ret and f is not None and getattr(f, 'size', 0) > 0:
                    frame = f.copy()
                temp_cap.release()
            except Exception:
                pass

    if frame is None:
        import numpy as np
        frame = np.zeros((360, 640, 3), dtype=np.uint8)
        cv2.putText(frame, f"LANE {lane_id} VIOLATION EVENT", (50, 180), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)

    h, w = frame.shape[:2]
    if h > 0 and w > 0:
        if w > 640:
            scale = 640.0 / w
            new_h = max(1, int(h * scale))
            frame = cv2.resize(frame, (640, new_h))

    if violation_detector:
        alert = violation_detector.simulate_manual_violation(frame, lane_id, db_conn_func=get_db)
        return jsonify({"success": True, "alert": alert})
    return jsonify({"error": "Violation detector not active"}), 500


# --- Run Application ---
if __name__ == '__main__':
    setup_database()  # This will create the DB and user on first run
    if not os.path.exists(UPLOAD_FOLDER):
        os.makedirs(UPLOAD_FOLDER)


    app.run(debug=False, host='0.0.0.0', threaded=True)
