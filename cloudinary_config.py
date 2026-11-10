import os
import io
import cv2

# Built-in lightweight .env loader (zero external dependencies required)
def _load_env_file(filepath):
    if not os.path.exists(filepath):
        return
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' in line:
                    key, val = line.split('=', 1)
                    key = key.strip()
                    val = val.strip().strip('"\'')
                    if key and key not in os.environ:
                        os.environ[key] = val
    except Exception as e:
        print(f"[Notice] Could not parse .env file: {e}")

# Load .env file from the current project directory
base_dir = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(base_dir, '.env')

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=env_path)
except ImportError:
    _load_env_file(env_path)

try:
    import cloudinary
    import cloudinary.uploader
    CLOUDINARY_AVAILABLE = True
except ImportError:
    cloudinary = None
    CLOUDINARY_AVAILABLE = False

CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME", "").strip()
API_KEY = os.getenv("CLOUDINARY_API_KEY", "").strip()
API_SECRET = os.getenv("CLOUDINARY_API_SECRET", "").strip()

if CLOUDINARY_AVAILABLE and CLOUD_NAME and API_KEY and API_SECRET and CLOUD_NAME != "your_cloud_name_here":
    try:
        cloudinary.config(
            cloud_name=CLOUD_NAME,
            api_key=API_KEY,
            api_secret=API_SECRET,
            secure=True
        )
        CLOUDINARY_CONFIGURED = True
    except Exception as e:
        print(f"[Cloudinary Config Error] {e}")
        CLOUDINARY_CONFIGURED = False
else:
    CLOUDINARY_CONFIGURED = False

def is_cloudinary_configured():
    """Checks if Cloudinary credentials are validly configured."""
    return CLOUDINARY_CONFIGURED

def upload_image_to_cloudinary(image_frame, public_id=None, folder="stms_violations"):
    """
    Directly uploads an OpenCV BGR image frame to Cloudinary without saving to local disk.
    Returns the secure HTTPS URL of the uploaded image on Cloudinary, or None if failed.
    """
    if not CLOUDINARY_AVAILABLE:
        print("[Cloudinary Error] Cloudinary package is not installed in this environment. Run: pip install cloudinary")
        return None

    if not is_cloudinary_configured():
        print("[Cloudinary Warning] Cloudinary credentials are not set in .env file! Please set CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, and CLOUDINARY_API_SECRET in stms/.env")
        return None

    try:
        # Encode image to JPEG format in memory (no disk I/O)
        ret, buffer = cv2.imencode('.jpg', image_frame, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
        if not ret:
            print("[Cloudinary Error] Failed to encode frame to JPEG.")
            return None

        # Convert to bytes buffer for in-memory stream upload
        image_bytes = io.BytesIO(buffer)

        upload_params = {
            "folder": folder,
            "resource_type": "image",
            "overwrite": True
        }
        if public_id:
            upload_params["public_id"] = public_id

        # Upload directly to Cloudinary
        response = cloudinary.uploader.upload(image_bytes, **upload_params)
        secure_url = response.get("secure_url")
        print(f"[Cloudinary] Successfully uploaded image to Cloudinary: {secure_url}")
        return secure_url
    except Exception as e:
        print(f"[Cloudinary Upload Error] {e}")
        return None
