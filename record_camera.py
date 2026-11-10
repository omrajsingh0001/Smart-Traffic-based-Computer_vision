import cv2
import time
import os
import requests
import threading
from datetime import datetime

def upload_in_background(filename, lane_id):
    url = 'http://127.0.0.1:5000/api/auto_upload'
    try:
        with open(filename, 'rb') as f:             
            response = requests.post(url, files={'video': f}, data={'lane_id': lane_id})
            if response.status_code == 200:
                print(f"[UPLOAD SUCCESS] Video sent to Server for Lane(s): {lane_id}")
            else:
                print(f"[UPLOAD ERROR] Server responded with code {response.status_code}: {response.text}")
    except Exception as e:
        print(f"[UPLOAD ERROR] Could not connect to server: {e}")

def record_continuous_chunks(save_folder="camera_recordings", chunk_duration=40):       
    if not os.path.exists(save_folder):
        os.makedirs(save_folder)        

    print("\n" + "="*50)
    print(" IP WEBCAM STREAM (BUG-FREE METHOD) ")
    print("="*50)                                                                                   
    
    ip_address = input("\nType the IP Address shown on the IP Webcam app :\n> ")
    if ip_address.strip() == "":
        print("No IP entered. Exiting.")  
        return

    lane_id = input("\nWhich Lane is this camera for? (Type 1, 2, 3, 4, or 'ALL') :\n> ")
    if lane_id.strip() == "":
        lane_id = "ALL"
        
    stream_url = f"http://{ip_address.strip()}:8080/video"                          
    print(f"\nConnecting to {stream_url} ...")               
    
    # IP Webcam sends standard raw video that OpenCV understands natively!
    cap = cv2.VideoCapture(stream_url)    

    if not cap.isOpened():
        print("CRITICAL ERROR: Could not connect. Make sure IP Webcam is open and 'Start server' is clicked.")
        return

    test_success, test_frame = cap.read()
    if not test_success or test_frame is None:
        print("CRITICAL ERROR: Connected, but no video received.")
        return

    frame_height, frame_width = test_frame.shape[:2]
    print(f"-> Successfully connected! Resolution: {frame_width}x{frame_height}")

    # Wi-Fi streams are usually 10-15 FPS. If we use 30, the video looks fast-forwarded and short!
    fps = 15.0
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')

    print(f"Starting continuous recording...")
    print("Press Ctrl+C in your terminal to stop recording.")

    try:
        while True:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{save_folder}/recording_{timestamp}.mp4"
            
            out = cv2.VideoWriter(filename, fourcc, fps, (frame_width, frame_height))
            print(f"Now recording: {filename}")

            start_time = time.time()
            frames_written = 0
            
            while (time.time() - start_time) < chunk_duration:
                success, frame = cap.read()
                if not success or frame is None:
                    break
                
                if frame.shape[:2] != (frame_height, frame_width):
                    frame = cv2.resize(frame, (frame_width, frame_height))

                cv2.imshow("Live Recording Feed", frame)
                cv2.waitKey(1)

                out.write(frame)
                frames_written += 1

            out.release()
            
            if frames_written == 0:
                os.remove(filename)
                break
            else:
                print(f"*** SAVED SUCCESSFULLY: {filename} ***")
                threading.Thread(target=upload_in_background, args=(filename, lane_id.strip())).start()

    except KeyboardInterrupt:
        print("\nContinuous recording stopped by user.")
        
    finally:                                                                    
        if cap:
            cap.release()
        cv2.destroyAllWindows()

if __name__ == "__main__":
    record_continuous_chunks()
