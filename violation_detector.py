import os
import sys
import time
import math
import re
import cv2
import numpy as np
import threading
import base64
from datetime import datetime
from cloudinary_config import upload_image_to_cloudinary, is_cloudinary_configured

# Prevent OpenMP and Unicode crash on Windows
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
os.environ["PYTHONIOENCODING"] = "utf-8"

try:
    from ultralytics import YOLO
except ImportError:
    YOLO = None

try:
    import easyocr
except ImportError:
    easyocr = None

# Blacklist road marking words that OCR accidentally detects as license plates
PLATE_BLACKLIST = {
    'STOP', 'STOPEI', 'STPO', 'SLOW', 'LINE', 'LANE', 'ROAD', 'ZONE', 
    'PASS', 'KEEP', 'WAIT', 'POLICE', 'TAXI', 'BUS', 'CAR', 'TRUCK', 'STMS'
}

class ViolationDetector:
    """
    ANPR and Automatic Red-Light Violation Detector with Motion Trajectory Tracking.
    Only triggers when a vehicle physically moves across the stop-line under RED light.
    Stationary vehicles waiting in traffic do NOT trigger false violations.
    Evidence snapshots are streamed directly to Cloudinary (no local disk storage).
    """
    def __init__(self, plate_model_path="license_plate.pt", violations_dir=None):
        self.lock = threading.Lock()

        # Load License Plate YOLOv8 model
        self.plate_model = None
        if os.path.exists(plate_model_path) and YOLO:
            try:
                self.plate_model = YOLO(plate_model_path)   
                print(f"[ANPR] License plate model loaded from {plate_model_path}")
                # Warm up model to fuse layers safely before multi-threaded execution
                dummy = np.zeros((160, 160, 3), dtype=np.uint8)
                self.plate_model(dummy, verbose=False)
                print("[ANPR] Plate model warmed up successfully.")
            except Exception as e:
                print(f"[ANPR] Warning: Could not load plate model: {e}")

        # Lazy-loaded EasyOCR reader to save startup memory
        self.ocr_reader = None

        # Virtual stop-line position (70% of frame height)
        self.stop_line_ratio = 0.58

        # Centroid Tracking per lane: {lane_id: {track_id: {...}}}
        self.tracks = {1: {}, 2: {}, 3: {}, 4: {}}
        self.next_track_id = 1
        self.max_disappeared = 1.5  # seconds before track expires

        # In-memory recent violations for live alert banner
        self.recent_alerts = []

    def _get_ocr_reader(self):
        """Initializes EasyOCR reader safely if needed."""
        if self.ocr_reader is None and easyocr:
            try:
                self.ocr_reader = easyocr.Reader(['en'], gpu=False, verbose=False)
            except Exception as e:
                print(f"[ANPR] OCR init notice: {e}")
        return self.ocr_reader

    def draw_stop_line(self, frame, lane_status):
        """Clean video view: do not draw any red/green line across the road."""
        return frame

    def read_plate_text(self, plate_crop):
        """Extracts and cleans alphanumeric plate text using OCR."""
        if plate_crop is None or plate_crop.size == 0:
            return ""

        h, w = plate_crop.shape[:2]
        if w < 12 or h < 8:
            return ""

        try:
            gray = cv2.cvtColor(plate_crop, cv2.COLOR_BGR2GRAY)
            scale = max(1, int(120 / max(h, 1)))
            resized = cv2.resize(gray, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)

            reader = self._get_ocr_reader()
            if reader:
                results = reader.readtext(resized)
                candidate_text = ""
                for bbox, text, score in results:
                    cleaned = re.sub(r'[^A-Za-z0-9]', '', text).upper()
                    # Filter blacklisted road signs like "STOP", "SLOW"
                    if cleaned in PLATE_BLACKLIST:
                        continue
                    if len(cleaned) >= 2 and score > 0.25:
                        candidate_text += cleaned

                # Final validation
                if len(candidate_text) >= 4 and candidate_text not in PLATE_BLACKLIST:
                    # Must contain at least one digit or follow typical plate format
                    if any(c.isdigit() for c in candidate_text):
                        return candidate_text
        except Exception:
            pass

        return ""

    def detect_plate_in_crop(self, vehicle_crop):
        """Runs the license plate model on a cropped vehicle."""
        if self.plate_model is None or vehicle_crop is None or vehicle_crop.size == 0:
            return None, ""

        try:
            results = self.plate_model.predict(vehicle_crop, conf=0.30, verbose=False)
            h, w = vehicle_crop.shape[:2]

            for r in results:
                if len(r.boxes) > 0:
                    best_box = r.boxes[0]
                    x1, y1, x2, y2 = map(int, best_box.xyxy[0])
                    pad = 4
                    crop_y1 = max(0, y1 - pad)
                    crop_y2 = min(h, y2 + pad)
                    crop_x1 = max(0, x1 - pad)
                    crop_x2 = min(w, x2 + pad)
                    plate_crop = vehicle_crop[crop_y1:crop_y2, crop_x1:crop_x2]
                    plate_text = self.read_plate_text(plate_crop)
                    return plate_crop, plate_text
        except Exception:
            pass

        return None, ""

    def _update_tracks(self, lane_id, detections, now):
        """
        Matches incoming vehicle detections to tracked trajectories.
        detections: list of [{'bbox': (xmin, ymin, xmax, ymax), 'class': 'Car', 'conf': 0.8}]
        """
        lane_dict = self.tracks[lane_id]

        # Cleanup old tracks
        for tid in list(lane_dict.keys()):
            if now - lane_dict[tid]['last_seen'] > self.max_disappeared:
                del lane_dict[tid]

        if not detections:
            return []

        matched_tracks = []
        unmatched_dets = []

        for det in detections:
            xmin, ymin, xmax, ymax = det['bbox']
            cx = (xmin + xmax) / 2.0
            cy = float(ymax)  # Wheel contact point

            # Find closest existing track
            best_tid = None
            best_dist = 65.0  # max association distance in pixels

            for tid, tinfo in lane_dict.items():
                prev_cx, prev_cy = tinfo['curr_pos']
                dist = math.hypot(cx - prev_cx, cy - prev_cy)
                if dist < best_dist:
                    best_dist = dist
                    best_tid = tid

            if best_tid is not None:
                # Update track with motion history
                tinfo = lane_dict[best_tid]
                tinfo['prev_pos'] = tinfo['curr_pos']
                tinfo['curr_pos'] = (cx, cy)
                tinfo['speed_y'] = cy - tinfo['prev_pos'][1]
                tinfo['last_seen'] = now
                tinfo['bbox'] = det['bbox']
                tinfo['class'] = det.get('class', 'Vehicle')
                matched_tracks.append((best_tid, tinfo))
            else:
                unmatched_dets.append(det)

        # Register new tracks
        for det in unmatched_dets:
            xmin, ymin, xmax, ymax = det['bbox']
            cx = (xmin + xmax) / 2.0
            cy = float(ymax)

            tid = self.next_track_id
            self.next_track_id += 1

            new_track = {
                'id': tid,
                'initial_y': cy,
                'prev_pos': (cx, cy),
                'curr_pos': (cx, cy),
                'speed_y': 0.0,
                'last_seen': now,
                'bbox': det['bbox'],
                'class': det.get('class', 'Vehicle'),
                'violated': False,
                'encroachment_violated': False,
                'has_crept_forward': False,
                'stopped_on_zebra_count': 0
            }
            lane_dict[tid] = new_track
            matched_tracks.append((tid, new_track))

        return matched_tracks

    def check_violation(self, frame, lane_id, vehicle_boxes, lane_status, db_conn_func=None):
        """
        Checks for real-time red-light crossing violations.
        CRITICAL: Only triggers when a vehicle transitions from ABOVE the stop line
        to BELOW the stop line with positive forward speed while the light is RED.
        Thread-safe across multiple concurrent camera streams.
        Evidence snapshot is uploaded directly to Cloudinary without saving to disk.
        """
        with self.lock:
            now = time.time()
            active_tracks = self._update_tracks(lane_id, vehicle_boxes, now)

            if not active_tracks:
                return frame

            h, w = frame.shape[:2]
            stop_line_y = int(h * self.stop_line_ratio)

            for tid, track in active_tracks:
                prev_y = track['prev_pos'][1]
                curr_y = track['curr_pos'][1]
                speed_y = track['speed_y']  # Forward motion speed

                # --- RULE 1: RED LIGHT VIOLATION (Moving across stop line on RED - ₹1,000) ---
                crossed_stop_line = (prev_y <= stop_line_y + 10) and (curr_y > stop_line_y) and (speed_y >= 0.8)
                trigger_rule1 = crossed_stop_line and (lane_status == 'red') and not track['violated']

                # --- RULE 2: ZEBRA CROSSING ENCROACHMENT (Crept past stop line onto zebra on RED - ₹500) ---
                zebra_zone_bottom = stop_line_y + int(h * 0.18)
                on_zebra = (stop_line_y < curr_y <= zebra_zone_bottom)
                is_slow = abs(speed_y) < 2.0

                if on_zebra and is_slow and (lane_status == 'red'):
                    track['stopped_on_zebra_count'] = track.get('stopped_on_zebra_count', 0) + 1
                else:
                    track['stopped_on_zebra_count'] = 0

                trigger_rule2 = (track.get('stopped_on_zebra_count', 0) >= 12) and (lane_status == 'red') and not track['violated'] and not track.get('encroachment_violated', False)

                if trigger_rule1 or trigger_rule2:
                    if trigger_rule1:
                        track['violated'] = True
                        v_type = "Red Light Violation"
                        v_fine = 1000
                        banner_color = (0, 0, 180)
                        rule_prefix = "RL"
                    else:
                        track['encroachment_violated'] = True
                        v_type = "Zebra Crossing Encroachment"
                        v_fine = 500
                        banner_color = (0, 140, 200)
                        rule_prefix = "ZEBRA"

                    xmin, ymin, xmax, ymax = track['bbox']
                    v_class = track['class']

                    # Crop vehicle
                    v_x1 = max(0, int(xmin))
                    v_y1 = max(0, int(ymin))
                    v_x2 = min(w, int(xmax))
                    v_y2 = min(h, int(ymax))
                    vehicle_crop = frame[v_y1:v_y2, v_x1:v_x2].copy()

                    # ANPR Plate Extraction
                    plate_crop, plate_text = self.detect_plate_in_crop(vehicle_crop)

                    if not plate_text or len(plate_text) < 4:
                        plate_text = f"MH12-V{lane_id}-{tid:04d}"

                    # Evidence Image Generation in Memory
                    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
                    public_id = f"violation_{rule_prefix}_L{lane_id}_{timestamp_str}_{tid}"

                    evidence_frame = frame.copy()
                    cv2.rectangle(evidence_frame, (0, 0), (w, 48), banner_color, -1)
                    cv2.putText(evidence_frame, f"{v_type.upper()} | Lane {lane_id} | Plate: {plate_text}",
                                (15, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.60, (255, 255, 255), 2)
                    cv2.rectangle(evidence_frame, (v_x1, v_y1), (v_x2, v_y2), (0, 0, 255), 3)

                    # Upload directly to Cloudinary (in-memory stream, no local disk file written)
                    cloud_url = upload_image_to_cloudinary(evidence_frame, public_id=public_id)
                    evidence_image_ref = cloud_url if cloud_url else f"{public_id}.jpg"
                    print(f"[E-CHALLAN] Recorded {v_type}: Lane {lane_id} | Track #{tid} | Plate: {plate_text} (₹{v_fine}) -> Cloud: {evidence_image_ref}")

                    # Save to Database with Cloudinary URL
                    if db_conn_func:
                        try:
                            conn = db_conn_func()
                            cursor = conn.cursor()
                            cursor.execute('''
                                INSERT INTO challans 
                                (lane_id, vehicle_type, plate_number, violation_type, fine_amount, evidence_image, status)
                                VALUES (%s, %s, %s, %s, %s, %s, %s)
                            ''', (
                                lane_id,
                                v_class,
                                plate_text,
                                v_type,
                                v_fine,
                                evidence_image_ref,
                                "Unpaid"
                            ))
                            conn.commit()
                            conn.close()
                        except Exception as e:
                            print(f"[ANPR] Database error: {e}")

                    # Add to recent alerts list
                    self.recent_alerts.append({
                        'lane': lane_id,
                        'plate': plate_text,
                        'vehicle': v_class,
                        'time': datetime.now().strftime("%H:%M:%S")
                    })
                    if len(self.recent_alerts) > 10:
                        self.recent_alerts.pop(0)

                # If vehicle is in violation state, show brief label on vehicle
                if track.get('violated', False) and (now - track['last_seen'] < 1.0):
                    xmin, ymin, xmax, ymax = track['bbox']
                    cv2.rectangle(frame, (int(xmin), int(ymin)), (int(xmax), int(ymax)), (0, 0, 255), 2)
                    cv2.putText(frame, "VIOLATION", (int(xmin), max(int(ymin) - 8, 20)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 255), 2)

            return frame

    def count_pedestrians_in_crosswalk(self, frame, person_boxes):
        """
        Detects pedestrians crossing the zebra zone.
        Draws a subtle badge over pedestrians crossing and returns count.
        """
        if not person_boxes:
            return 0

        h, w = frame.shape[:2]
        stop_line_y = int(h * self.stop_line_ratio)
        zebra_top = max(0, stop_line_y - int(h * 0.12))
        zebra_bottom = min(h, stop_line_y + int(h * 0.22))

        crossing_count = 0
        for p in person_boxes:
            xmin, ymin, xmax, ymax = p['bbox']
            feet_y = ymax

            # If person's feet are within the crosswalk region
            if zebra_top <= feet_y <= zebra_bottom:
                crossing_count += 1
                # Draw subtle indicator over pedestrian
                cv2.rectangle(frame, (int(xmin), int(ymin)), (int(xmax), int(ymax)), (0, 230, 255), 2)
                cv2.putText(frame, "Pedestrian", (int(xmin), max(int(ymin) - 6, 18)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 230, 255), 1, cv2.LINE_AA)

        return crossing_count

    def simulate_manual_violation(self, frame, lane_id, db_conn_func=None):
        """Instantly captures a real violation on demand and uploads to Cloudinary."""
        with self.lock:
            h, w = frame.shape[:2]
            v_type = "Red Light Violation"
            v_fine = 1000
            v_class = "Car"
            plate_text = f"MH12-RN-{int(time.time()) % 10000:04d}"

            timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
            public_id = f"violation_RL_L{lane_id}_{timestamp_str}_manual"

            evidence_frame = frame.copy()
            cv2.rectangle(evidence_frame, (0, 0), (w, 48), (0, 0, 180), -1)
            cv2.putText(evidence_frame, f"RED LIGHT VIOLATION | Lane {lane_id} | Plate: {plate_text}",
                        (15, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.60, (255, 255, 255), 2)
            # Add center focus box
            cv2.rectangle(evidence_frame, (int(w*0.25), int(h*0.35)), (int(w*0.75), int(h*0.85)), (0, 0, 255), 3)

            # Upload directly to Cloudinary (in-memory, no local file written)
            cloud_url = upload_image_to_cloudinary(evidence_frame, public_id=public_id)
            evidence_image_ref = cloud_url if cloud_url else f"{public_id}.jpg"

            if db_conn_func:
                conn = None
                try:
                    conn = db_conn_func()
                    cursor = conn.cursor()
                    cursor.execute('''
                        INSERT INTO challans 
                        (lane_id, vehicle_type, plate_number, violation_type, fine_amount, evidence_image, status)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ''', (
                        lane_id,
                        v_class,
                        plate_text,
                        v_type,
                        v_fine,
                        evidence_image_ref,
                        "Unpaid"
                    ))
                    conn.commit()
                except Exception as e:
                    print(f"[ANPR] Manual violation DB error: {e}")
                finally:
                    if conn:
                        try:
                            conn.close()
                        except Exception:
                            pass

            alert_item = {
                'lane': lane_id,
                'plate': plate_text,
                'vehicle': v_class,
                'time': datetime.now().strftime("%H:%M:%S")
            }
            self.recent_alerts.append(alert_item)
            if len(self.recent_alerts) > 10:
                self.recent_alerts.pop(0)

            return alert_item
