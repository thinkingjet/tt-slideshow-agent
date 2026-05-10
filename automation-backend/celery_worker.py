from celery import Celery
import redis
import os
import cv2
import numpy as np
import tempfile
import json
import requests
from typing import Optional, Set, Dict, List
from pathlib import Path
import time
import subprocess
import threading
from datetime import datetime, timedelta

# Import Supabase integration for production
try:
    from supabase import create_client, Client
    SUPABASE_AVAILABLE = True
except ImportError:
    print("Warning: Supabase client not available. Install with: pip install supabase")
    SUPABASE_AVAILABLE = False

try:
    import boto3
    from botocore.exceptions import NoCredentialsError
    BOTO3_AVAILABLE = True
except ImportError:
    print("Warning: Boto3 client not available. Install with: pip install boto3")
    BOTO3_AVAILABLE = False

# Import hook+demo processor
from hook_demo_processor import process_hook_demo_video

# Leonardo API configuration (used by other features)
LEONARDO_API_KEY = os.getenv('LEONARDO_API_KEY')
LEONARDO_API_URL = 'https://cloud.leonardo.ai/api/rest/v1'

# Replicate API configuration (for Seedance)
REPLICATE_API_KEY = os.getenv('REPLICATE_API_KEY')
REPLICATE_API_URL = 'https://api.replicate.com/v1'

# Avatar video request model
class AvatarVideoRequest:
    def __init__(self, data: dict):
        self.job_id = data.get('jobId')
        self.avatar_id = data.get('avatarId')
        self.image_url = data.get('imageUrl')
        self.video_prompt = data.get('videoPrompt')
        self.user_id = data.get('userId')

# Redis connection
REDIS_URL = os.getenv('REDIS_URL', 'redis://localhost:6379/0')
redis_client = redis.from_url(REDIS_URL)

# Global set to track currently processing jobs (prevents duplicates)
currently_processing_jobs: Set[str] = set()
processing_jobs_lock = threading.Lock()

# Background thread for periodic orphaned job checks
recovery_thread = None
recovery_stop_event = threading.Event()

# Initialize Celery with FIFO configuration
celery_app = Celery(
    'video_processing',
    broker=REDIS_URL,
    backend=REDIS_URL
)

# CRITICAL: Configure Celery for FIFO (First In, First Out) processing
celery_app.conf.update(
    # Worker configuration for FIFO
    task_acks_late=True,  # Acknowledge tasks only after completion
    worker_prefetch_multiplier=1,  # Process one task at a time per worker
    
    # Queue configuration for FIFO
    task_routes={
        'process_video_async': {'queue': 'video_processing', 'routing_key': 'video_processing'},
        'process_hook_demo_async': {'queue': 'video_processing', 'routing_key': 'video_processing'},
        'process_avatar_video_async': {'queue': 'video_processing', 'routing_key': 'video_processing'},
        'process_slideshow_async': {'queue': 'video_processing', 'routing_key': 'video_processing'}
    },
    
    # Priority configuration (lower number = higher priority)
    task_default_priority=5,
    worker_hijack_root_logger=False,
    
    # Ensure tasks are processed in the order they arrive
    task_inherit_parent_priority=True,
    
    # Result settings
    result_expires=3600,  # Results expire in 1 hour
    task_compression='gzip',
    result_compression='gzip',
    
    # Serialization
    task_serializer='json',
    result_serializer='json',
    accept_content=['json'],
    
    # Timezone
    timezone='UTC',
    enable_utc=True,
)

# Initialize Supabase client (for production)
supabase_client = None
if SUPABASE_AVAILABLE:
    supabase_url = os.getenv('NEXT_PUBLIC_SUPABASE_URL')
    supabase_key = os.getenv('SUPABASE_SERVICE_ROLE_KEY')
    
    if supabase_url and supabase_key:
        try:
            supabase_client = create_client(supabase_url, supabase_key)
            print("Supabase client initialized for production video uploads")
        except Exception as e:
            print(f"Failed to initialize Supabase client: {e}")

# Check if we're in production mode
IS_PRODUCTION = os.getenv('NODE_ENV') == 'production' or os.getenv('ENVIRONMENT') == 'production'

def add_job_to_processing(job_id: str) -> bool:
    """Add job to currently processing set. Returns False if already processing."""
    with processing_jobs_lock:
        if job_id in currently_processing_jobs:
            return False
        currently_processing_jobs.add(job_id)
        return True

def remove_job_from_processing(job_id: str):
    """Remove job from currently processing set."""
    with processing_jobs_lock:
        currently_processing_jobs.discard(job_id)

def get_orphaned_processing_jobs() -> List[Dict]:
    """Get jobs that are stuck in 'processing' state from Supabase."""
    if not supabase_client:
        print("Supabase client not available for orphaned job recovery")
        return []
    
    try:
        # Get all videos with 'processing' status older than 10 minutes
        # (gives time for normal processing, but catches truly orphaned jobs)
        cutoff_time = datetime.now() - timedelta(minutes=10)
        cutoff_time_str = cutoff_time.isoformat()
        
        result = supabase_client.table('user_videos').select(
            'id, user_id, job_id, video_type, title, original_meme_filename, caption_used, '
            'background_type, background_reference, text_position, created_at, recovery_metadata'
        ).eq('video_url', 'processing').lt('created_at', cutoff_time_str).execute()
        
        if result.data:
            print(f"Found {len(result.data)} potentially orphaned processing jobs")
            return result.data
        else:
            return []
            
    except Exception as e:
        print(f"Error checking for orphaned jobs: {e}")
        return []

def recover_orphaned_job(job_data: Dict):
    """Attempt to recover an orphaned processing job."""
    job_id = job_data.get('job_id')
    user_id = job_data.get('user_id')
    video_type = job_data.get('video_type', 'greenscreen')
    
    if not job_id or not user_id:
        print(f"Invalid job data for recovery: {job_data}")
        return
    
    # Check if this job is already being processed
    if not add_job_to_processing(job_id):
        print(f"Job {job_id} is already being processed, skipping recovery")
        return
    
    try:
        print(f"Attempting to recover orphaned job: {job_id} (type: {video_type})")
        
        if video_type == 'ugc':  # Hook+demo videos
            # For hook+demo videos, we need the original request data from recovery_metadata
            recovery_metadata = job_data.get('recovery_metadata')
            
            if recovery_metadata and recovery_metadata.get('hookVideoUrl') and recovery_metadata.get('demoVideoUrl'):
                print(f"Attempting to restart hook+demo processing for {job_id}")
                
                # Create request data for reprocessing using stored recovery data
                request_data = {
                    'jobId': job_id,
                    'hookVideoUrl': recovery_metadata['hookVideoUrl'],
                    'demoVideoUrl': recovery_metadata['demoVideoUrl'],
                    'caption': recovery_metadata.get('caption', ''),
                    'musicName': recovery_metadata.get('musicName', ''),
                    'textPosition': recovery_metadata.get('textPosition', 'top'),
                    'userId': user_id
                }
                
                # Queue the job for reprocessing
                process_hook_demo_async.apply_async(args=[request_data], priority=1)  # High priority for recovery
                print(f"Queued orphaned hook+demo job {job_id} for reprocessing")
                
            else:
                print(f"Insufficient recovery data for hook+demo job {job_id}, marking as failed")
                mark_job_as_failed(user_id, job_id, "Processing was interrupted and cannot be recovered. Please try again.")
            
        elif video_type == 'greenscreen':
            # For green screen videos, we have enough data to potentially recover
            # Check if we have the original meme file
            original_filename = job_data.get('original_meme_filename')
            caption = job_data.get('caption_used')
            background_type = job_data.get('background_type')
            background_reference = job_data.get('background_reference')
            text_position = job_data.get('text_position', 'middle')
            
            if original_filename and caption:
                print(f"Attempting to restart green screen processing for {job_id}")
                
                # Create request data for reprocessing
                request_data = {
                    'jobId': job_id,
                    'videoFilename': original_filename,
                    'caption': caption,
                    'userId': user_id,
                    'textPosition': text_position,
                    'backgroundImagePath': None,  # Will use URL-based backgrounds
                    'userBackgroundUrl': background_reference if background_type == 'user' else None,
                    'defaultBackgroundUrl': background_reference if background_type == 'default' else None
                }
                
                # Queue the job for reprocessing
                process_video_async.apply_async(args=[request_data], priority=1)  # High priority for recovery
                print(f"Queued orphaned green screen job {job_id} for reprocessing")
                
            else:
                print(f"Insufficient data to recover green screen job {job_id}, marking as failed")
                mark_job_as_failed(user_id, job_id, "Processing was interrupted and cannot be recovered. Please try again.")
        else:
            print(f"Unknown video type {video_type} for job {job_id}, marking as failed")
            mark_job_as_failed(user_id, job_id, "Processing was interrupted. Please try again.")
            
    except Exception as e:
        print(f"Error recovering orphaned job {job_id}: {e}")
        mark_job_as_failed(user_id, job_id, f"Recovery failed: {str(e)}")
    finally:
        remove_job_from_processing(job_id)

def mark_job_as_failed(user_id: str, job_id: str, error_message: str):
    """Mark a job as failed in the database."""
    if not supabase_client:
        return
    
    try:
        # Update the video entry to indicate failure
        result = supabase_client.table('user_videos').update({
            'video_url': f'failed: {error_message}',
            'updated_at': datetime.now().isoformat()
        }).eq('job_id', job_id).eq('user_id', user_id).execute()
        
        # Also update Redis with failure status
        update_job_progress(job_id, 'error', 0, error_message)
        
        print(f"Marked job {job_id} as failed: {error_message}")
        
    except Exception as e:
        print(f"Error marking job {job_id} as failed: {e}")

def check_and_recover_orphaned_jobs():
    """Check for and recover orphaned processing jobs."""
    print("Checking for orphaned processing jobs...")
    
    orphaned_jobs = get_orphaned_processing_jobs()
    
    if orphaned_jobs:
        print(f"Found {len(orphaned_jobs)} orphaned jobs, attempting recovery...")
        
        for job_data in orphaned_jobs:
            recover_orphaned_job(job_data)
    else:
        print("No orphaned jobs found")

def start_recovery_monitor():
    """Start the background thread that periodically checks for orphaned jobs."""
    global recovery_thread
    
    if recovery_thread and recovery_thread.is_alive():
        print("Recovery monitor already running")
        return
    
    def recovery_loop():
        """Background loop that checks for orphaned jobs every 3 minutes."""
        print("Started orphaned job recovery monitor")
        
        # Initial check on startup (after 30 seconds to let worker settle)
        time.sleep(30)
        check_and_recover_orphaned_jobs()
        
        # Then check every 3 minutes
        while not recovery_stop_event.wait(180):  # 180 seconds = 3 minutes
            try:
                check_and_recover_orphaned_jobs()
            except Exception as e:
                print(f"Error in recovery monitor: {e}")
        
        print("Recovery monitor stopped")
    
    recovery_thread = threading.Thread(target=recovery_loop, daemon=True)
    recovery_thread.start()

def stop_recovery_monitor():
    """Stop the background recovery monitor."""
    global recovery_thread
    
    if recovery_thread and recovery_thread.is_alive():
        print("Stopping recovery monitor...")
        recovery_stop_event.set()
        recovery_thread.join(timeout=5)
        print("Recovery monitor stopped")

# IMPORTANT: Recovery monitor is now only started by the API server to prevent race conditions
# 
# Previously, each worker would start its own recovery monitor, which could cause multiple
# workers to simultaneously detect and try to recover the same orphaned job. This could
# lead to duplicate processing of the same job.
#
# Now, only the API server runs the recovery monitor and assigns orphaned jobs back to
# the worker queue, ensuring single-point coordination and no race conditions.
print("Orphaned job recovery system disabled in workers (handled by API server only)")

def extract_timestamp_from_job_id(job_id: str) -> int:
    """Extract timestamp from job ID for FIFO ordering"""
    try:
        # Job ID format: job_timestamp_randomstring
        parts = job_id.split('_')
        if len(parts) >= 2:
            return int(parts[1])
        return 0
    except (ValueError, IndexError):
        return 0

def update_job_progress(job_id: str, status: str, progress: int, message: str, result_url: str = None):
    """Update job progress in Redis and publish to subscribers"""
    try:
        progress_data = {
            'status': status,
            'progress': progress,
            'message': message,
            'job_id': job_id,
            'timestamp': time.time()
        }
        
        if result_url:
            progress_data['result_url'] = result_url
        
        # Store in Redis
        progress_key = f"job_progress:{job_id}"
        redis_client.setex(progress_key, 3600, json.dumps(progress_data))  # Expire in 1 hour
        
        # Publish update to subscribers
        redis_client.publish(f"job_updates:{job_id}", json.dumps(progress_data))
        
        print(f"Job {job_id}: {status} - {progress}% - {message}")
        
    except Exception as e:
        print(f"Error updating job progress: {e}")

def remove_green_screen(video_path: str, output_path: str, background_image_path: Optional[str] = None, 
                       user_background_url: Optional[str] = None, default_background_url: Optional[str] = None):
    """Remove green screen from video and optionally add background"""
    print(f"Processing video: {video_path}")
    print(f"Background image path: {background_image_path}")
    print(f"User background URL: {user_background_url}")
    print(f"Default background URL: {default_background_url}")
    
    cap = cv2.VideoCapture(video_path)
    
    # Get original video properties
    fps = int(cap.get(cv2.CAP_PROP_FPS))
    original_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    original_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    
    # Instagram Reels dimensions (9:16 aspect ratio)
    target_width = 1080
    target_height = 1920
    
    # Define codec and create VideoWriter
    fourcc = cv2.VideoWriter_fourcc(*'XVID')
    out = cv2.VideoWriter(output_path, fourcc, fps, (target_width, target_height))
    
    if not out.isOpened():
        print(f"Failed to open VideoWriter with XVID, trying mp4v...")
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        out = cv2.VideoWriter(output_path, fourcc, fps, (target_width, target_height))
        if not out.isOpened():
            raise Exception("Failed to create VideoWriter with any codec")
    
    # Load and prepare background image
    background = None
    
    # Priority: 1. Custom uploaded image, 2. User background URL, 3. Default background URL
    if background_image_path and os.path.exists(background_image_path):
        background = cv2.imread(background_image_path)
        background = cv2.resize(background, (target_width, target_height))
        print("Using custom uploaded background image")
    elif user_background_url:
        try:
            print(f"Loading user background from URL: {user_background_url}")
            response = requests.get(user_background_url)
            response.raise_for_status()
            
            with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as temp_bg:
                temp_bg.write(response.content)
                temp_bg_path = temp_bg.name
            
            background = cv2.imread(temp_bg_path)
            if background is not None:
                background = cv2.resize(background, (target_width, target_height))
                print("Successfully loaded user background from URL")
            
            os.unlink(temp_bg_path)
            
        except Exception as e:
            print(f"Error downloading user background: {e}")
            background = None
    elif default_background_url:
        try:
            print(f"Loading default background from URL: {default_background_url}")
            response = requests.get(default_background_url)
            response.raise_for_status()
            
            with tempfile.NamedTemporaryFile(delete=False, suffix='.png') as temp_bg:
                temp_bg.write(response.content)
                temp_bg_path = temp_bg.name
            
            background = cv2.imread(temp_bg_path)
            if background is not None:
                background = cv2.resize(background, (target_width, target_height))
                print("Successfully loaded default background from URL")
            
            os.unlink(temp_bg_path)
            
        except Exception as e:
            print(f"Error downloading default background: {e}")
            background = None
    
    # If no background loaded, create fallback gradient
    if background is None:
        background = np.zeros((target_height, target_width, 3), dtype=np.uint8)
        start_color = [245, 180, 180]  # Rose-100
        end_color = [252, 200, 200]    # Rose-200
        
        for y in range(target_height):
            ratio = y / target_height
            b = int(start_color[0] + ratio * (end_color[0] - start_color[0]))
            g = int(start_color[1] + ratio * (end_color[1] - start_color[1]))
            r = int(start_color[2] + ratio * (end_color[2] - start_color[2]))
            background[y, :] = [b, g, r]
        print("Using fallback gradient background")
    else:
        print("Using loaded background image")
    
    frame_count = 0
    print(f"Starting to process {total_frames} frames...")
    
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        
        # Convert to HSV for better green screen detection
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        
        # Define range for green color (adjusted for better detection)
        lower_green = np.array([35, 40, 40])
        upper_green = np.array([85, 255, 255])
        
        # Create mask for green pixels
        mask = cv2.inRange(hsv, lower_green, upper_green)
        
        # Morphological operations to clean up the mask
        kernel = np.ones((3,3), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        
        # Gaussian blur to smooth edges
        mask = cv2.GaussianBlur(mask, (5, 5), 0)
        
        # Calculate scaling to fit frame in target dimensions while maintaining aspect ratio
        scale_x = target_width / original_width
        scale_y = target_height / original_height
        scale = min(scale_x, scale_y)
        
        new_width = int(original_width * scale)
        new_height = int(original_height * scale)
        
        # Resize frame and mask
        frame_resized = cv2.resize(frame, (new_width, new_height))
        mask_resized = cv2.resize(mask, (new_width, new_height))
        
        # Create final frame with background
        result = background.copy()
        
        # Calculate position to center the video
        y_offset = (target_height - new_height) // 2
        x_offset = (target_width - new_width) // 2
        
        # Normalize mask to 0-1 range
        mask_norm = mask_resized.astype(float) / 255
        
        # Apply the processed frame to the background
        for c in range(3):
            result[y_offset:y_offset+new_height, x_offset:x_offset+new_width, c] = (
                frame_resized[:, :, c] * (1 - mask_norm) + 
                background[y_offset:y_offset+new_height, x_offset:x_offset+new_width, c] * mask_norm
            )
        
        # Write the result frame
        if result is not None and result.shape[0] > 0 and result.shape[1] > 0:
            out.write(result)
        
        frame_count += 1
        
        # Yield progress occasionally
        if frame_count % 10 == 0:
            progress = int((frame_count / total_frames) * 50)  # 50% for green screen removal
            yield progress
    
    cap.release()
    out.release()
    print(f"Green screen removal completed. Processed {frame_count} frames.")

def add_text_to_video(input_path: str, output_path: str, caption: str, text_position: str = "middle", highlight: bool = False):
    """Add text overlay to video using OpenCV matching frontend styling exactly"""
    cap = cv2.VideoCapture(input_path)
    
    # Get video properties
    fps = int(cap.get(cv2.CAP_PROP_FPS))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    
    print(f"Video dimensions: {width}x{height}")
    
    # Define codec and create VideoWriter
    fourcc = cv2.VideoWriter_fourcc(*'XVID')
    out = cv2.VideoWriter(output_path, fourcc, fps, (width, height))
    
    if not out.isOpened():
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        out = cv2.VideoWriter(output_path, fourcc, fps, (width, height))
        if not out.isOpened():
            raise Exception("Failed to create VideoWriter for text overlay")
    
    # Clean and normalize the caption text to handle encoding issues
    # Replace problematic characters that OpenCV might not render correctly
    caption = str(caption).encode('ascii', 'ignore').decode('ascii')
    
    # Replace common Unicode characters with ASCII equivalents
    caption = caption.replace('\u2019', "'")  # Unicode apostrophe to ASCII apostrophe
    caption = caption.replace('\u201c', '"')  # Unicode left double quote
    caption = caption.replace('\u201d', '"')  # Unicode right double quote
    caption = caption.replace('\u2013', '-')  # En dash to hyphen
    caption = caption.replace('\u2014', '-')  # Em dash to hyphen
    caption = caption.replace('???', "'")     # Question marks to apostrophe
    
    print(f"Processed caption: {caption}")
    
    # EXACT FRONTEND MATCH: text-xl = 1.25rem = 20px base size
    # However, OpenCV renders much smaller than CSS, so we need to scale up significantly
    # For 1080px width video, use much larger base size to match visual appearance
    base_font_size_px = 60  # Increased significantly to match preview size
    scale_factor = width / 1080.0  # Assuming 1080px is standard width
    scaled_font_size = base_font_size_px * scale_factor
    
    # OpenCV font scale calculation - FONT_HERSHEY_DUPLEX needs larger scale for proper size
    # Increased scale to make text much more prominent and match preview
    font_scale = scaled_font_size / 30.0
    
    # EXACT FRONTEND MATCH: font-bold - Use thicker text for bold appearance
    # Adjust thickness based on video size for consistent appearance
    base_thickness = 4  # Increased thickness for better bold appearance and visibility
    thickness = max(2, int(base_thickness * scale_factor))
    
    # EXACT FRONTEND MATCH: px-6 = 24px horizontal padding on each side
    # Scale padding proportionally to video width
    base_padding_px = 24  # Frontend px-6 equivalent
    horizontal_padding = int(base_padding_px * scale_factor)
    
    # EXACT FRONTEND MATCH: width: 95% - Text container uses 95% of video width
    container_width = int(width * 0.95)  # 95% of total width
    text_start_x = (width - container_width) // 2  # Center the 95% container
    effective_width = container_width - (2 * horizontal_padding)  # Available width after padding
    
    # Split caption into lines with more accurate width calculation
    words = caption.split()
    lines = []
    current_line = ""
    
    for word in words:
        test_line = current_line + (" " + word if current_line else word)
        # Test if the line would fit within the effective width
        (test_width, _), _ = cv2.getTextSize(test_line, cv2.FONT_HERSHEY_DUPLEX, font_scale, thickness)
        if test_width <= effective_width:
            current_line = test_line
        else:
            if current_line:
                lines.append(current_line)
            current_line = word
    
    if current_line:
        lines.append(current_line)
    
    # EXACT FRONTEND MATCH: line-height: 1.2
    line_height = int(scaled_font_size * 1.2)
    total_text_height = len(lines) * line_height
    
    print(f"Text split into {len(lines)} lines: {lines}")
    
    # Exact positioning to match frontend positions
    vertical_padding = int(height * 0.05)  # 5% padding from top/bottom to match frontend positioning
    
    if text_position.lower() == "top":
        # Match frontend's top positioning with 5% from top
        start_y = vertical_padding + line_height
    elif text_position.lower() == "bottom":
        # Match frontend's bottom positioning with 5% from bottom
        start_y = height - vertical_padding - total_text_height + line_height
    else:  # middle (default)
        # Match frontend's vertical centering
        start_y = (height - total_text_height) // 2 + line_height
    
    frame_count = 0
    
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        
        # Draw each line with EXACT frontend text-shadow match
        for i, line in enumerate(lines):
            if not line.strip():  # Skip empty lines
                continue
                
            y_position = start_y + (i * line_height)
            
            # Center text horizontally within the 95% container, respecting px-6 padding
            (text_width, text_height), _ = cv2.getTextSize(line, cv2.FONT_HERSHEY_DUPLEX, font_scale, thickness)
            x_position = text_start_x + horizontal_padding + (effective_width - text_width) // 2
            
            # Ensure text doesn't go outside frame bounds
            y_position = max(line_height, min(y_position, height - 10))
            x_position = max(10, min(x_position, width - text_width - 10))
            
            if highlight:
                pad_y = int(line_height * 0.25)
                pad_x = int(horizontal_padding * 0.25)
                top_left = (max(0, x_position - pad_x), max(0, y_position - text_height - pad_y))
                bottom_right = (min(width, x_position + text_width + pad_x), min(height, y_position + pad_y))
                radius = int(min(text_height + 2 * pad_y, (bottom_right[1] - top_left[1])) * 0.5)
                cv2.rectangle(frame, (top_left[0] + radius, top_left[1]), (bottom_right[0] - radius, bottom_right[1]), (255,255,255), -1)
                cv2.rectangle(frame, (top_left[0], top_left[1] + radius), (bottom_right[0], bottom_right[1] - radius), (255,255,255), -1)
                cv2.circle(frame, (top_left[0] + radius, top_left[1] + radius), radius, (255,255,255), -1, lineType=cv2.LINE_AA)
                cv2.circle(frame, (bottom_right[0] - radius, top_left[1] + radius), radius, (255,255,255), -1, lineType=cv2.LINE_AA)
                cv2.circle(frame, (top_left[0] + radius, bottom_right[1] - radius), radius, (255,255,255), -1, lineType=cv2.LINE_AA)
                cv2.circle(frame, (bottom_right[0] - radius, bottom_right[1] - radius), radius, (255,255,255), -1, lineType=cv2.LINE_AA)

            if not highlight:
                shadow_offsets = [(0,1),(0,-1),(1,0),(-1,0)]
                for dx, dy in shadow_offsets:
                    cv2.putText(frame, line, (x_position + dx, y_position + dy), cv2.FONT_HERSHEY_DUPLEX, font_scale, (0,0,0), thickness, cv2.LINE_AA)

            cv2.putText(frame, line, (x_position, y_position), cv2.FONT_HERSHEY_DUPLEX, font_scale, (0,0,0) if highlight else (255,255,255), thickness, cv2.LINE_AA)
        
        out.write(frame)
        frame_count += 1
        
        # Update progress occasionally
        if frame_count % 10 == 0:
            progress = int((frame_count / total_frames) * 50)  # 50% for text overlay
            yield progress
    
    cap.release()
    out.release()
    print(f"Text overlay completed. Processed {frame_count} frames with font scale {font_scale}")

def merge_audio_with_ffmpeg(original_video_path: str, processed_video_path: str, output_path: str):
    """Merge audio from original video with processed video using FFmpeg"""
    try:
        cmd = [
            'ffmpeg',
            '-i', processed_video_path,
            '-i', original_video_path,
            '-c:v', 'libx264',
            '-preset', 'medium',
            '-crf', '23',
            '-c:a', 'aac',
            '-b:a', '128k',
            '-map', '0:v:0',
            '-map', '1:a:0',
            '-shortest',
            '-movflags', '+faststart',
            '-y',
            output_path
        ]
        
        print(f"Running FFmpeg command: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            print(f"FFmpeg error: {result.stderr}")
            # Fallback: copy processed video without audio
            import shutil
            shutil.copy2(processed_video_path, output_path)
        else:
            print("FFmpeg completed successfully")
        
    except Exception as e:
        print(f"Error in merge_audio_with_ffmpeg: {e}")
        import shutil
        shutil.copy2(processed_video_path, output_path)

def upload_video_to_supabase_sync(video_path: str, user_id: str, job_id: str) -> Optional[str]:
    """Upload video to Supabase Storage and return public URL (synchronous)"""
    if not supabase_client:
        print("Supabase client not available, skipping upload")
        return None
    
    try:
        timestamp = int(time.time())
        filename = f"{user_id}/{timestamp}_{job_id}_final.mp4"
        
        with open(video_path, 'rb') as f:
            video_data = f.read()
        
        print(f"Uploading video to Supabase: {filename}")
        result = supabase_client.storage.from_("user-videos").upload(
            filename, 
            video_data,
            file_options={"content-type": "video/mp4"}
        )
        
        public_url = supabase_client.storage.from_("user-videos").get_public_url(filename)
        
        if public_url:
            # Handle both string and dict responses from get_public_url
            if isinstance(public_url, dict):
                public_url_str = public_url.get('publicUrl', '') + '?'
            else:
                public_url_str = str(public_url) + '?'
            print(f"Video uploaded successfully: {public_url_str}")
            return public_url_str
        else:
            print("Failed to get public URL")
            return None
            
    except Exception as e:
        print(f"Error uploading to Supabase: {e}")
        return None

def create_processing_entry_sync(user_id: str, job_id: str, metadata: dict) -> Optional[str]:
    """Create immediate database entry with 'processing' status (synchronous)"""
    if not supabase_client:
        print("Supabase client not available, skipping processing entry creation")
        return None
    
    try:
        video_record = {
            'user_id': user_id,
            'title': metadata.get('title', 'Generated Video'),
            'description': metadata.get('description', ''),
            'video_url': 'processing',  # Special marker for processing state
            'thumbnail_url': None,
            'original_meme_filename': metadata.get('original_filename'),
            'caption_used': metadata.get('caption'),
            'background_type': metadata.get('background_type', 'default'),
            'background_reference': metadata.get('background_reference'),
            'text_position': metadata.get('text_position', 'middle'),
            'video_type': 'greenscreen',  # Set video type for green screen memes
            'file_size': None,
            'duration': None,
            'job_id': job_id  # Store job_id for easy reference
        }
        
        result = supabase_client.table('user_videos').insert(video_record).execute()
        if result.data and len(result.data) > 0:
            record_id = result.data[0]['id']
            print(f"Processing entry created successfully with ID: {record_id}")
            return record_id
        else:
            print("Failed to create processing entry")
            return None
        
    except Exception as e:
        print(f"Error creating processing entry: {e}")
        return None

def update_video_entry_with_url_sync(user_id: str, job_id: str, video_url: str):
    """Update existing database entry with actual video URL (synchronous)"""
    if not supabase_client:
        print("Supabase client not available, skipping video URL update")
        return
    
    try:
        # Find the record by user_id and job_id where video_url is 'processing'
        result = supabase_client.table('user_videos').update({
            'video_url': video_url
        }).eq('user_id', user_id).eq('job_id', job_id).eq('video_url', 'processing').execute()
        
        if result.data and len(result.data) > 0:
            print(f"Video URL updated successfully for job {job_id}: {video_url}")
        else:
            print(f"No processing entry found to update for job {job_id}")
        
    except Exception as e:
        print(f"Error updating video URL: {e}")

@celery_app.task(bind=True, name='process_video_async')
def process_video_async(self, request_data):
    """Process video asynchronously with FIFO ordering"""
    job_id = request_data['jobId']
    
    # Check for duplicate processing - prevent same job from running twice
    if not add_job_to_processing(job_id):
        print(f"Job {job_id} is already being processed, skipping duplicate")
        return {'status': 'duplicate', 'message': 'Job already being processed'}
    
    # Extract timestamp for FIFO verification
    timestamp = extract_timestamp_from_job_id(job_id)
    
    print(f"DEBUG: video_dir = /app/green_screen_videos")
    print(f"DEBUG: videoFilename = {request_data['videoFilename']}")
    
    try:
        # Setup paths
        video_dir = Path("/app/green_screen_videos")
        video_path = video_dir / request_data['videoFilename']
        
        print(f"DEBUG: video_path = {video_path}")
        print(f"DEBUG: video_path.exists() = {video_path.exists()}")
        
        # Use temp directory in production, public/generated in development
        if IS_PRODUCTION:
            output_dir = Path(tempfile.gettempdir()) / f"video_processing_{job_id}"
        else:
            output_dir = Path("/app/public/generated")
        
        output_dir.mkdir(exist_ok=True)
        
        temp_video_path = output_dir / f"{job_id}_temp.mp4"
        final_video_path = output_dir / f"{job_id}_final.mp4"
        
        # Validate input video exists
        if not video_path.exists():
            update_job_progress(job_id, 'error', 0, 'Video file not found')
            return {'status': 'error', 'message': 'Video file not found'}
        
        # Step 1: Remove green screen
        update_job_progress(job_id, 'processing', 20, 'Removing green screen...')
        
        for progress in remove_green_screen(
            str(video_path), 
            str(temp_video_path), 
            request_data.get('backgroundImagePath'),
            request_data.get('userBackgroundUrl'),
            request_data.get('defaultBackgroundUrl')
        ):
            update_job_progress(job_id, 'processing', 20 + progress, 'Processing green screen...')
        
        # Step 2: Add text caption
        update_job_progress(job_id, 'processing', 70, 'Adding text caption...')
        
        temp_video_with_text = output_dir / f"{job_id}_with_text.mp4"
        for progress in add_text_to_video(
            str(temp_video_path),
            str(temp_video_with_text),
            request_data['caption'],
            request_data.get('textPosition', 'middle'),
            bool(request_data.get('highlightText', False))
        ):
            update_job_progress(job_id, 'processing', 70 + progress, 'Adding text...')
        
        # Step 3: Merge audio from original video
        update_job_progress(job_id, 'processing', 85, 'Adding audio...')
        
        merge_audio_with_ffmpeg(str(video_path), str(temp_video_with_text), str(final_video_path))
        
        # Clean up temp files
        if temp_video_path.exists():
            temp_video_path.unlink()
        if temp_video_with_text.exists():
            temp_video_with_text.unlink()
        
        # Step 4: Upload to R2 if user ID provided
        if request_data.get('userId'):
            update_job_progress(job_id, 'processing', 95, 'Uploading to cloud storage...')
            
            # Use the new R2 upload function
            cdn_url = upload_video_to_r2_sync(str(final_video_path), request_data['userId'], job_id)
            
            if cdn_url:
                # Update existing processing entry with actual video URL
                update_video_entry_with_url_sync(request_data['userId'], job_id, cdn_url)
                
                # Clean up local file in production
                if IS_PRODUCTION and final_video_path.exists():
                        final_video_path.unlink()
                
                update_job_progress(job_id, 'completed', 100, 'Video processing complete!', cdn_url)
                return {'status': 'completed', 'result_url': cdn_url}
            else:
                update_job_progress(job_id, 'error', 0, 'Failed to upload video to cloud storage')
                return {'status': 'error', 'message': 'Failed to upload video to cloud storage'}
        else:
            # Fallback: Use local file path when no user ID
            result_url = f"/generated/{job_id}_final.mp4"
            update_job_progress(job_id, 'completed', 100, 'Video processing complete!', result_url)
            return {'status': 'completed', 'result_url': result_url}
        
    except Exception as e:
        error_message = f"Processing failed: {str(e)}"
        update_job_progress(job_id, 'error', 0, error_message)
        return {'status': 'error', 'message': error_message}
    finally:
        # Always remove from processing set when done
        remove_job_from_processing(job_id)

@celery_app.task(name='get_job_status')
def get_job_status(job_id: str):
    """Get current status of a processing job"""
    try:
        progress_key = f"job_progress:{job_id}"
        progress_data = redis_client.get(progress_key)
        
        if progress_data:
            return json.loads(progress_data)
        else:
            return {'status': 'not_found', 'progress': 0, 'message': 'Job not found'}
    except Exception as e:
        return {'status': 'error', 'progress': 0, 'message': f'Error getting status: {str(e)}'}

@celery_app.task(bind=True, name='process_hook_demo_async')
def process_hook_demo_async(self, request_data):
    """Process hook+demo video asynchronously with FIFO ordering"""
    job_id = request_data['jobId']
    
    # Check for duplicate processing - prevent same job from running twice
    if not add_job_to_processing(job_id):
        print(f"Hook+demo job {job_id} is already being processed, skipping duplicate")
        return {'status': 'duplicate', 'message': 'Job already being processed'}
    
    # Extract timestamp for FIFO verification
    timestamp = extract_timestamp_from_job_id(job_id)
    
    print(f"Starting hook+demo processing for job {job_id}")
    
    try:
        # Update job progress
        update_job_progress(job_id, 'processing', 10, 'Starting hook+demo video processing...')
        
        # Processing entry is now created in the frontend API route (like green screen memes)
        # No need to create it here, just start processing
        
        update_job_progress(job_id, 'processing', 20, 'Downloading hook video...')
        
        # Create temp directory for this job
        temp_dir = Path(tempfile.gettempdir()) / f"hook_demo_{job_id}"
        temp_dir.mkdir(exist_ok=True)
        
        # Download hook video
        hook_video_url = request_data['hookVideoUrl']
        hook_temp_path = temp_dir / f"hook_{job_id}.mp4"
        
        print(f"Downloading video from: {hook_video_url}")
        response = requests.get(hook_video_url)
        response.raise_for_status()
        with open(hook_temp_path, 'wb') as f:
            f.write(response.content)
        print(f"Video downloaded to: {hook_temp_path}")
        
        # Download demo video only if provided
        demo_video_url = request_data['demoVideoUrl']
        demo_temp_path = None
        
        if demo_video_url and demo_video_url.strip():
            update_job_progress(job_id, 'processing', 30, 'Downloading demo video...')
            demo_temp_path = temp_dir / f"demo_{job_id}.mp4"
            
            print(f"Downloading video from: {demo_video_url}")
            response = requests.get(demo_video_url)
            response.raise_for_status()
            with open(demo_temp_path, 'wb') as f:
                f.write(response.content)
            print(f"Video downloaded to: {demo_temp_path}")
        else:
            print("No demo video URL provided - will process hook video only")
            update_job_progress(job_id, 'processing', 30, 'No demo video - processing hook video only...')
        
        update_job_progress(job_id, 'processing', 40, 'Getting music file...')
        
        # Get music file based on user selection
        from hook_demo_processor import get_music_path
        music_name = request_data.get('musicName', '')
        music_path = None
        
        # Only get music if a specific music name was provided
        if music_name and music_name.strip():
            music_path = get_music_path(music_name)
            
            # If specific music was requested but not found, try random as fallback
            if not music_path:
                print(f"Music '{music_name}' not found, falling back to random selection")
                from hook_demo_processor import get_random_music_file
                music_path = get_random_music_file()
        else:
            print("No music selected - will create video without background music")
            music_path = None
        
        update_job_progress(job_id, 'processing', 50, 'Adding caption to hook video...')
        
        # Process the video
        final_output_path = temp_dir / f"final_{job_id}.mp4"
        
        from hook_demo_processor import process_hook_demo_video
        
        # Process with progress updates
        try:
            print(f"Highlight flag received: {request_data.get('highlightText', False)}")
        except Exception:
            pass
        for progress in process_hook_demo_video(
            hook_video_path=str(hook_temp_path),
            demo_video_path=str(demo_temp_path) if demo_temp_path else None,
            music_path=music_path,
            caption=request_data['caption'],
            output_path=str(final_output_path),
            text_position=request_data.get('textPosition', 'top'),  # Default to 'top' to match frontend
            highlight_text=bool(request_data.get('highlightText', False))
        ):
            # Map processing progress to 50-85%
            mapped_progress = 50 + int(progress * 0.35)
            update_job_progress(job_id, 'processing', mapped_progress, f'Processing video... {progress}%')
        
        update_job_progress(job_id, 'processing', 85, 'Uploading to cloud storage...')
        
        # Upload to R2 if user ID provided
        if request_data.get('userId'):
            # Use the new R2 upload function
            cdn_url = upload_video_to_r2_sync(str(final_output_path), request_data['userId'], job_id)
            
            if cdn_url:
                # Update existing processing entry with actual video URL
                update_video_entry_with_url_sync(request_data['userId'], job_id, cdn_url)

                # Update file size as well (optional but good practice)
                try:
                    file_size = final_output_path.stat().st_size if final_output_path.exists() else None
                    if file_size and supabase_client:
                        supabase_client.table('user_videos').update({
                            'file_size': file_size
                        }).eq('job_id', job_id).execute()
                except Exception as e:
                    print(f"Warning: Could not update file size for job {job_id}: {e}")

                update_job_progress(job_id, 'completed', 100, 'Hook+demo video processing completed!', cdn_url)
                return {'status': 'completed', 'result_url': cdn_url}
            else:
                update_job_progress(job_id, 'error', 0, 'Failed to upload video to cloud storage')
                # Mark as failed in DB
                mark_job_as_failed(request_data['userId'], job_id, "Failed to upload to cloud storage.")
                return {'status': 'error', 'message': 'Failed to upload video to cloud storage'}
        else:
            # Fallback for local development without user context
            result_url = f"/generated/final_{job_id}.mp4"
            import shutil
            public_dir = Path("/app/public/generated")
            public_dir.mkdir(exist_ok=True)
            shutil.copy(final_output_path, public_dir / f"final_{job_id}.mp4")
            update_job_progress(job_id, 'completed', 100, 'Hook+demo video processing completed!', result_url)
            return {'status': 'completed', 'result_url': result_url}
        
    except Exception as e:
        error_message = f"Error processing hook+demo video: {str(e)}"
        print(error_message)
        update_job_progress(job_id, 'error', 0, error_message)
        return {'status': 'error', 'message': 'Failed to process hook+demo video'}
    
    finally:
        # Cleanup temporary files
        update_job_progress(job_id, 'processing', 95, 'Cleaning up temporary files...')
        
        try:
            if 'temp_dir' in locals() and temp_dir.exists():
                import shutil
                shutil.rmtree(temp_dir)
                print(f"Cleaned up temp directory: {temp_dir}")
        except Exception as e:
            print(f"Error during cleanup: {e}")
        
        # Always remove from processing set when done
        remove_job_from_processing(job_id)

def upload_avatar_video_to_r2_sync(video_path: str, user_id: str, generation_id: str) -> Optional[str]:
    """Upload avatar video to R2 Storage (user-avatar-videos bucket) and return public CDN URL."""
    if not BOTO3_AVAILABLE:
        print("Boto3 client not available, skipping R2 upload")
        return None

    try:
        r2_account_id = os.getenv('R2_ACCOUNT_ID')
        r2_access_key_id = os.getenv('R2_ACCESS_KEY_ID')
        r2_secret_access_key = os.getenv('R2_SECRET_ACCESS_KEY')
        r2_custom_domain = os.getenv('R2_CUSTOM_DOMAIN', 'cdn.reelstacks.ai').strip('\'"')
        bucket_name = 'user-avatar-videos'  # Dedicated bucket for avatar videos

        if not all([r2_account_id, r2_access_key_id, r2_secret_access_key]):
            print("R2 credentials not fully configured, skipping upload.")
            return None

        s3_client = boto3.client(
            's3',
            endpoint_url=f'https://{r2_account_id}.r2.cloudflarestorage.com',
            aws_access_key_id=r2_access_key_id,
            aws_secret_access_key=r2_secret_access_key,
            region_name='auto'
        )
        
        timestamp = int(time.time())
        filename = f"{user_id}/{generation_id}_{timestamp}.mp4"

        print(f"Uploading avatar video to R2: {filename}")
        s3_client.upload_file(
            video_path,
            bucket_name,
            filename,
            ExtraArgs={
                'ContentType': 'video/mp4',
                'CacheControl': 'public, max-age=31536000, immutable'
            }
        )
        
        # Construct the public CDN URL
        public_url = f"https://{r2_custom_domain}/{bucket_name}/{filename}"
        print(f"Avatar video uploaded to R2 successfully: {public_url}")
        return public_url

    except NoCredentialsError:
        print("Boto3: Credentials not available for R2 upload.")
        return None
    except Exception as e:
        print(f"Error uploading avatar video to R2: {e}")
        return None

def upload_video_to_r2_sync(video_path: str, user_id: str, job_id: str) -> Optional[str]:
    """Upload video to R2 Storage and return public CDN URL."""
    if not BOTO3_AVAILABLE:
        print("Boto3 client not available, skipping R2 upload")
        return None

    try:
        r2_account_id = os.getenv('R2_ACCOUNT_ID')
        r2_access_key_id = os.getenv('R2_ACCESS_KEY_ID')
        r2_secret_access_key = os.getenv('R2_SECRET_ACCESS_KEY')
        r2_custom_domain = os.getenv('R2_CUSTOM_DOMAIN', 'cdn.reelstacks.ai').strip('\'"')
        bucket_name = 'user-videos'

        if not all([r2_account_id, r2_access_key_id, r2_secret_access_key]):
            print("R2 credentials not fully configured, skipping upload.")
            return None

        s3_client = boto3.client(
            's3',
            endpoint_url=f'https://{r2_account_id}.r2.cloudflarestorage.com',
            aws_access_key_id=r2_access_key_id,
            aws_secret_access_key=r2_secret_access_key,
            region_name='auto'
        )
        
        timestamp = int(time.time())
        filename = f"{user_id}/{timestamp}_{job_id}_final.mp4"

        print(f"Uploading video to R2: {filename}")
        s3_client.upload_file(
            video_path,
            bucket_name,
            filename,
            ExtraArgs={
                'ContentType': 'video/mp4',
                'CacheControl': 'public, max-age=31536000, immutable'
            }
        )
        
        # Construct the public CDN URL
        public_url = f"https://{r2_custom_domain}/{bucket_name}/{filename}"
        print(f"Video uploaded to R2 successfully: {public_url}")
        return public_url

    except NoCredentialsError:
        print("Boto3: Credentials not available for R2 upload.")
        return None
    except Exception as e:
        print(f"Error uploading to R2: {e}")
        return None

@celery_app.task(bind=True, name='process_avatar_video_async')
def process_avatar_video_async(self, request_data):
    """Process avatar video generation using Replicate Seedance with FIFO queue support."""
    
    job_id = request_data.get('jobId')
    print(f"\n🎭 Starting avatar video processing for job: {job_id}")
    
    # Prevent duplicate processing
    if not add_job_to_processing(job_id):
        print(f"Job {job_id} is already being processed, skipping duplicate")
        return {"status": "duplicate", "job_id": job_id}
    
    try:
        # Parse request data
        avatar_request = AvatarVideoRequest(request_data)
        
        if not all([avatar_request.job_id, avatar_request.avatar_id, avatar_request.image_url, 
                   avatar_request.video_prompt, avatar_request.user_id]):
            raise ValueError("Missing required fields in avatar video request")
        
        # Update progress
        self.update_state(state='PROGRESS', meta={'progress': 10, 'message': 'Starting Reelstacks processing...'})
        
        # Step 1: Start Seedance prediction (image-to-video)
        print(f"Starting Seedance image-to-video with image: {avatar_request.image_url}")
        prediction_id = start_seedance_video_generation(avatar_request.image_url, avatar_request.video_prompt)

        if not prediction_id:
            raise Exception("Failed to start Seedance video generation")

        # Update progress
        self.update_state(state='PROGRESS', meta={'progress': 50, 'message': 'Video generation started, waiting for completion...'})

        # Step 2: Poll for completion
        print(f"Polling Seedance for completion: {prediction_id}")
        video_url = poll_seedance_video_status(prediction_id, self)
        # Use prediction_id as generation identifier in our DB
        generation_id = prediction_id
        
        if not video_url:
            raise Exception("Video generation failed or timed out")
        
        # Update progress
        self.update_state(state='PROGRESS', meta={'progress': 80, 'message': 'Video generated, downloading and uploading to R2...'})
        
        # Step 4: Download video and upload to R2
        print(f"Video generated by provider: {video_url}")
        print("Downloading video and uploading to R2...")
        
        # Download the video from source CDN
        import tempfile
        import requests
        
        with tempfile.NamedTemporaryFile(delete=False, suffix='.mp4') as temp_file:
            temp_path = temp_file.name
            
            try:
                # Download the video
                response = requests.get(video_url, timeout=300)  # 5 minute timeout
                response.raise_for_status()
                temp_file.write(response.content)
                temp_file.flush()
                
                print(f"Video downloaded to temporary file: {temp_path}")
                
                # Upload to R2
                r2_url = upload_avatar_video_to_r2_sync(temp_path, avatar_request.user_id, generation_id)
                
                if r2_url:
                    print(f"✅ Video uploaded to R2 successfully: {r2_url}")
                    final_video_url = r2_url
                else:
                    print("⚠️ Failed to upload to R2, falling back to source URL")
                    final_video_url = video_url
                    
            except Exception as download_error:
                print(f"❌ Error downloading/uploading video: {download_error}")
                print("Falling back to source URL")
                final_video_url = video_url
            finally:
                # Clean up temporary file
                try:
                    import os
                    os.unlink(temp_path)
                except:
                    pass
        
        # Update progress
        self.update_state(state='PROGRESS', meta={'progress': 95, 'message': 'Updating database...'})
        
        # Step 5: Update database with final video URL (R2 or provider fallback)
        if supabase_client:
            update_avatar_video_completion(avatar_request.avatar_id, avatar_request.user_id, 
                                         generation_id, final_video_url, avatar_request.video_prompt)
        
        print(f"✅ Avatar video processing completed successfully: {final_video_url}")
        
        # Clean up processing state
        remove_job_from_processing(job_id)
        
        return {
            "status": "completed",
            "job_id": job_id,
            "generation_id": generation_id,
            "video_url": final_video_url,
            "message": "Avatar video processing completed successfully"
        }
        
    except Exception as e:
        print(f"❌ Error processing avatar video {job_id}: {str(e)}")
        
        # Clean up processing state
        remove_job_from_processing(job_id)
        
        # Re-raise the exception for Celery to handle
        raise self.retry(exc=e, countdown=60, max_retries=3)

def upload_image_to_leonardo(image_url: str) -> Optional[str]:
    """Upload image to Leonardo AI and return init image ID."""
    try:
        if not LEONARDO_API_KEY:
            print("Leonardo API key not configured")
            return None
        
        # Fetch the image
        image_response = requests.get(image_url)
        if not image_response.ok:
            print(f"Failed to fetch image from URL: {image_url}")
            return None
        
        image_blob = image_response.content
        
        # Get presigned URL from Leonardo
        presigned_response = requests.post(
            f"{LEONARDO_API_URL}/init-image",
            headers={
                'Authorization': f'Bearer {LEONARDO_API_KEY}',
                'Content-Type': 'application/json',
            },
            json={'extension': 'jpg'}
        )
        
        if not presigned_response.ok:
            print(f"Failed to get presigned URL: {presigned_response.text}")
            return None
        
        presigned_data = presigned_response.json()
        upload_data = presigned_data.get('uploadInitImage')
        
        if not upload_data:
            print("No upload data received from Leonardo")
            return None
        
        # Parse fields
        fields = upload_data.get('fields')
        if isinstance(fields, str):
            fields = json.loads(fields)
        
        # Prepare form data for S3 upload
        form_data = {}
        if upload_data.get('key'):
            form_data['key'] = upload_data['key']
        elif fields and fields.get('key'):
            form_data['key'] = fields['key']
        
        if fields:
            for key, value in fields.items():
                if key != 'key':
                    form_data[key] = value
        
        # Upload to S3
        files = {'file': ('avatar.jpg', image_blob, 'image/jpeg')}
        upload_response = requests.post(upload_data['url'], data=form_data, files=files)
        
        if not upload_response.ok:
            print(f"Failed to upload image to S3: {upload_response.text}")
            return None
        
        return upload_data.get('id')
        
    except Exception as e:
        print(f"Error uploading image to Leonardo: {e}")
        return None

def start_seedance_video_generation(image_url: str, video_prompt: str) -> Optional[str]:
    """Start image-to-video generation on Replicate Seedance-1 Lite and return prediction ID."""
    try:
        if not REPLICATE_API_KEY:
            print("Replicate API key not configured")
            return None

        # Construct prompt stabilizing camera as before
        # prompt_prefix = "This is a selfie-style portrait of a person. Animate the person to "
        # prompt_suffix = " The camera should remain stable with only very subtle slow horizontal panning left or right if any movement at all. No vertical camera movement, no zooming, no shaking. Keep the person centered and in frame at all times. Focus on animating the person, not the camera."
        prompt_prefix=" "
        prompt_suffix=" "
        structured_prompt = f"{prompt_prefix}{video_prompt}{prompt_suffix}"

        payload = {
            "input": {
                "prompt": structured_prompt,
                "image": image_url,
                "duration": 5,
                "resolution": "720p",
                "aspect_ratio": "9:16",
                "camera_fixed": True,
                "fps": 24
            }
        }

        response = requests.post(
            f"{REPLICATE_API_URL}/models/bytedance/seedance-1-lite/predictions",
            headers={
                'Authorization': f'Bearer {REPLICATE_API_KEY}',
                'Content-Type': 'application/json',
                'Prefer': 'wait=60'
            },
            json=payload
        )

        if not response.ok:
            print(f"Failed to start Seedance video generation: {response.text}")
            return None

        data = response.json()
        prediction_id = data.get('id')
        print(f"Seedance prediction started with ID: {prediction_id}")
        return prediction_id

    except Exception as e:
        print(f"Error starting Seedance generation: {e}")
        return None

def poll_seedance_video_status(prediction_id: str, task_instance) -> Optional[str]:
    """Poll Replicate for completion and return video URL."""
    try:
        if not REPLICATE_API_KEY:
            print("Replicate API key not configured")
            return None

        max_attempts = 240  # ~20 minutes at 5s intervals
        attempt = 0

        def extract_url(output):
            try:
                if not output:
                    return None
                if isinstance(output, str):
                    return output
                if isinstance(output, list) and output:
                    first = output[0]
                    if isinstance(first, str):
                        return first
                    if isinstance(first, dict) and first.get('url'):
                        return first['url']
                if isinstance(output, dict) and output.get('url'):
                    return output['url']
            except Exception:
                return None
            return None

        while attempt < max_attempts:
            response = requests.get(
                f"{REPLICATE_API_URL}/predictions/{prediction_id}",
                headers={'Authorization': f'Bearer {REPLICATE_API_KEY}'}
            )

            if not response.ok:
                print(f"Failed to check Seedance prediction: {response.text}")
                time.sleep(5)
                attempt += 1
                continue

            data = response.json()
            status = data.get('status')
            print(f"Seedance status: {status} (attempt {attempt + 1}/{max_attempts})")

            if status == 'succeeded':
                video_url = extract_url(data.get('output'))
                if video_url:
                    print(f"Seedance video completed: {video_url}")
                    return video_url
                print("Succeeded but no output URL found")
                return None
            if status in ['failed', 'canceled']:
                print(f"Seedance video failed: {data}")
                return None

            progress = min(50 + (attempt * 30 // max_attempts), 75)
            task_instance.update_state(
                state='PROGRESS', 
                meta={'progress': progress, 'message': f'Generating video... ({attempt + 1}/{max_attempts})'}
            )

            time.sleep(5)
            attempt += 1

        print(f"Seedance video generation timed out after {max_attempts} attempts")
        return None

    except Exception as e:
        print(f"Error polling Seedance status: {e}")
        return None

def update_avatar_generation_status(avatar_id: str, user_id: str, generation_id: Optional[str], 
                                  status: str, video_prompt: str):
    """Update avatar generation status in database."""
    try:
        if not supabase_client:
            print("Supabase client not available")
            return
        
        update_data = {
            'video_generation_status': status,
            'video_prompt': video_prompt,
            'updated_at': datetime.utcnow().isoformat()
        }
        
        if generation_id:
            update_data['video_generation_id'] = generation_id
        
        result = supabase_client.table('user_generated_avatars').update(update_data).eq('id', avatar_id).eq('user_id', user_id).execute()
        
        if result.data:
            print(f"Updated avatar {avatar_id} status to {status}")
        else:
            print(f"Failed to update avatar {avatar_id} status")
            
    except Exception as e:
        print(f"Error updating avatar status: {e}")

def update_avatar_video_completion(avatar_id: str, user_id: str, generation_id: str, 
                                 video_url: str, video_prompt: str):
    """Update avatar with completed video URL in video_urls array."""
    try:
        if not supabase_client:
            print("Supabase client not available")
            return
        
        # Get current avatar data
        result = supabase_client.table('user_generated_avatars').select('video_urls').eq('id', avatar_id).eq('user_id', user_id).single().execute()
        
        current_videos = result.data.get('video_urls', []) if result.data else []
        
        # Create new video entry
        new_video = {
            'url': video_url,
            'prompt': video_prompt,
            'created_at': datetime.utcnow().isoformat(),
            'generation_id': generation_id
        }
        
        # Find and replace the processing entry (matching by prompt and processing status)
        updated_videos = []
        processing_entry_replaced = False
        
        for video in current_videos:
            # Replace processing entry that matches the prompt
            if (video.get('url') == 'processing' and 
                video.get('prompt') == video_prompt and 
                not processing_entry_replaced):
                updated_videos.append(new_video)
                processing_entry_replaced = True
                print(f"Replaced processing entry with final video URL")
            else:
                updated_videos.append(video)
        
        # If no processing entry was found to replace, add as new entry
        if not processing_entry_replaced:
            updated_videos.append(new_video)
            print(f"Added new video entry (no processing entry found to replace)")
        
        # Update database - only update video_urls and timestamp
        update_result = supabase_client.table('user_generated_avatars').update({
            'video_urls': updated_videos,
            'updated_at': datetime.utcnow().isoformat()
        }).eq('id', avatar_id).eq('user_id', user_id).execute()
        
        if update_result.data:
            print(f"Successfully updated avatar {avatar_id} with video URL")
        else:
            print(f"Failed to update avatar {avatar_id} with video URL")
            
    except Exception as e:
        print(f"Error updating avatar video completion: {e}")

@celery_app.task(bind=True, name='process_slideshow_async')
def process_slideshow_async(self, request_data):
    """Process slideshow images with text overlays asynchronously with FIFO ordering"""
    job_id = request_data['jobId']
    print(f"[SLIDESHOW_WORKER] Received job: {job_id}")
    job_id = request_data['jobId']
    
    # Check for duplicate processing - prevent same job from running twice
    if not add_job_to_processing(job_id):
        print(f"Slideshow job {job_id} is already being processed, skipping duplicate")
        return {'status': 'duplicate', 'message': 'Job already being processed'}
    
    # Extract timestamp for FIFO verification
    timestamp = extract_timestamp_from_job_id(job_id)
    
    print(f"Starting slideshow processing for job {job_id}")
    
    try:
        user_id = request_data['userId']
        slideshow_id = request_data['slideshowId']
        slideshow_data = request_data['slideshowData']
        title = request_data['title']
        
        # Update job progress
        update_job_progress(job_id, 'processing', 5, 'Starting slideshow image processing...')
        
        slides = slideshow_data.get('slides', [])
        total_slides = len(slides)
        
        if total_slides == 0:
            raise Exception("No slides found in slideshow data")
        
        update_job_progress(job_id, 'processing', 10, f'Processing {total_slides} slides...')
        
        generated_images = []
        
        # Process each slide
        for slide_index, slide in enumerate(slides):
            print(f"[SLIDESHOW_WORKER] Processing slide {slide_index + 1}/{total_slides} for job {job_id}")
            print(f"[SLIDESHOW_WORKER] Slide data: {json.dumps(slide, indent=2)}")
            try:
                print(f"Processing slide {slide_index + 1}/{total_slides}")
                
                # Update progress for current slide
                progress = 10 + (slide_index / total_slides) * 80  # 10-90% for slide processing
                update_job_progress(job_id, 'processing', int(progress), 
                                  f'Processing slide {slide_index + 1} of {total_slides}...')
                
                # Generate image with text overlay
                image_result = process_single_slide_image(slide, user_id, job_id, slide_index)
                
                if image_result:
                    generated_images.append({
                        'slide_id': slide.get('id', f'slide_{slide_index}'),
                        'slide_number': slide_index + 1,
                        'image_url': image_result['image_url'],
                        'original_image_url': slide.get('imageUrl'),
                        'text_elements': slide.get('textElements', []),
                        'created_at': time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())
                    })
                    
                    # Update database with current progress
                    update_slideshow_progress(user_id, job_id, slide_index + 1, total_slides, generated_images)
                else:
                    raise Exception(f"Failed to process slide {slide_index + 1}")
                    
            except Exception as slide_error:
                print(f"Error processing slide {slide_index + 1}: {slide_error}")
                # Continue with other slides but log the error
                generated_images.append({
                    'slide_id': slide.get('id', f'slide_{slide_index}'),
                    'slide_number': slide_index + 1,
                    'error': str(slide_error),
                    'original_image_url': slide.get('imageUrl'),
                    'created_at': time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())
                })
        
        update_job_progress(job_id, 'processing', 95, 'Finalizing slideshow...')
        
        # Update final database entry
        mark_slideshow_completed(user_id, job_id, generated_images)
        
        update_job_progress(job_id, 'completed', 100, 'Slideshow processing completed!', None)
        print(f"[SLIDESHOW_WORKER] Job {job_id} completed successfully.")
        
        return {
            'status': 'completed',
            'job_id': job_id,
            'generated_images': generated_images,
            'total_slides': total_slides,
            'message': 'Slideshow processing completed successfully'
        }
        
    except Exception as e:
        error_message = f"Error processing slideshow: {str(e)}"
        print(error_message)
        update_job_progress(job_id, 'error', 0, error_message)
        
        # Mark as failed in database
        mark_slideshow_failed(request_data['userId'], job_id, error_message)
        
        return {'status': 'error', 'message': error_message}
    
    finally:
        # Always remove from processing set when done
        remove_job_from_processing(job_id)

def process_single_slide_image(slide, user_id, job_id, slide_index):
    """Process a single slide image with text overlays - HIGH QUALITY rendering"""
    try:
        import tempfile
        import requests
        from PIL import Image, ImageDraw, ImageFont
        import io
        
        # Download the original image
        image_url = slide.get('imageUrl')
        if not image_url:
            raise Exception("No image URL found in slide")
        
        print(f"Downloading image: {image_url}")
        response = requests.get(image_url)
        response.raise_for_status()
        
        # Open image with PIL
        img = Image.open(io.BytesIO(response.content))
        
        # Convert to RGB if necessary
        if img.mode != 'RGB':
            img = img.convert('RGB')
        
        # CRITICAL HIGH-QUALITY APPROACH:
        # 1. Work at 3x resolution (1152x1440) for crisp text rendering
        # 2. Add text overlays at high resolution with 3x scaled fonts
        # 3. Keep final output at high resolution for maximum quality (no downscaling)
        
        # Target dimensions: 384x480 (frontend preview size)
        target_width, target_height = 384, 480
        
        # High-resolution rendering: 3x scale for crisp text
        scale_factor = 3
        hires_width = target_width * scale_factor  # 1152
        hires_height = target_height * scale_factor  # 1440
        
        print(f"Original image size: {img.size}")
        print(f"High-res processing size: {hires_width}x{hires_height} (scale: {scale_factor}x)")
        
        # Resize using object-cover behavior to MATCH frontend `object-cover`
        # 1) Scale image to fill target area while preserving aspect ratio
        # 2) Center-crop to exact target dimensions
        orig_w, orig_h = img.size
        target_w, target_h = hires_width, hires_height
        scale = max(target_w / float(orig_w), target_h / float(orig_h))
        resized_w = int(round(orig_w * scale))
        resized_h = int(round(orig_h * scale))
        print(f"Object-cover resize: orig={orig_w}x{orig_h}, target={target_w}x{target_h}, scale={scale:.4f}, resized={resized_w}x{resized_h}")

        img_resized = img.resize((resized_w, resized_h), Image.Resampling.LANCZOS)

        # Center crop to target size
        left = max(0, (resized_w - target_w) // 2)
        top = max(0, (resized_h - target_h) // 2)
        right = left + target_w
        bottom = top + target_h
        img_hires = img_resized.crop((left, top, right, bottom))
        print(f"High-resolution image created (object-cover cropped): {img_hires.size} from box ({left},{top},{right},{bottom})")
        
        # Get text elements - handle both camelCase and snake_case property names
        text_elements = slide.get('textElements', []) or slide.get('text_elements', [])
        
        print(f"Processing slide {slide_index}: found {len(text_elements)} text elements")
        print(f"Slide keys: {list(slide.keys())}")
        for i, elem in enumerate(text_elements):
            print(f"  Text element {i}: {elem}")
        
        if text_elements:
            # Process each text element at HIGH RESOLUTION
            print(f"=== PROCESSING {len(text_elements)} TEXT ELEMENTS AT HIGH-RES ===")
            for i, text_elem in enumerate(text_elements):
                print(f"--- HIGH-RES TEXT ELEMENT {i+1} ---")
                print(f"Text: '{text_elem.get('text', '')}'")
                print(f"Position: {text_elem.get('position', {})}")
                print(f"Font size: {text_elem.get('font_size', 'not set')}")
                print(f"Width: {text_elem.get('width', 'not set')}")
                print(f"Height: {text_elem.get('height', 'not set')}")
                print(f"Full element: {text_elem}")
                print(f"-------------------")
                
                # Add text overlay at high resolution with scale factor
                add_text_overlay_to_image_hires(img_hires, text_elem, scale_factor)
                
            print(f"=== ALL HIGH-RES TEXT ELEMENTS PROCESSED ===")
        else:
            print("No text elements to process")
        
        # KEEP HIGH-RESOLUTION: No downscaling - maintain 3x quality for crisp output
        # Final image stays at high resolution: 1152x1440 (3x scale, 4:5 ratio)
        print(f"Keeping high-resolution image at {img_hires.size} for maximum quality")
        print(f"Final output dimensions: {hires_width}x{hires_height} (3x scale, 4:5 ratio)")
        
        # Save high-resolution processed image to temporary file
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as temp_file:
            img_hires.save(temp_file, format='PNG', optimize=True, quality=95)
            temp_path = temp_file.name
        
        try:
            # Upload to R2
            timestamp = int(time.time())
            filename = f"{user_id}/{job_id}/slide_{slide_index}_{timestamp}.png"
            
            image_url = upload_image_to_r2_sync(temp_path, filename)
            
            if image_url:
                return {
                    'image_url': image_url,
                    'filename': filename
                }
            else:
                raise Exception("Failed to upload image to R2")
                
        finally:
            # Clean up temp file
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        
    except Exception as e:
        print(f"Error processing slide image: {e}")
        raise

def wrap_text_to_width(text, font_size, max_width):
    """
    Advanced text wrapping that exactly mimics CSS hyphens: auto and word-break: break-word
    to match the frontend preview behavior.
    
    Frontend CSS properties replicated:
    - wordWrap: 'break-word'
    - wordBreak: 'break-word' 
    - hyphens: 'auto'
    - whiteSpace: 'pre-wrap'
    - lineHeight: '1.2'
    - font-family: 'Inter'
    """
    try:
        # Character width for Inter font (Google Font used in frontend)
        # Inter has specific metrics - this is calibrated to match CSS rendering
        char_width_estimate = font_size * 0.48  # More accurate Inter font width ratio to match CSS
        max_chars_per_line = int(max_width / char_width_estimate)
        
        # Apply minimal safety buffer to account for font rendering differences
        max_chars_per_line = int(max_chars_per_line * 0.95)  # 5% buffer for safety
        
        print(f"Text wrapping calculation: font_size={font_size}, max_width={max_width}")
        print(f"Char width estimate: {char_width_estimate:.2f}px, max chars per line: {max_chars_per_line}")
        
        if max_chars_per_line <= 0:
            print(f"⚠️  Container too narrow for text wrapping (max_chars_per_line={max_chars_per_line})")
            return text
        
        # Don't wrap very short text 
        if len(text) <= max_chars_per_line:
            print(f"Text '{text}' is short enough ({len(text)} chars), no wrapping needed")
            return text
        
        # Split by existing line breaks first (preserve pre-wrap behavior)
        paragraphs = text.split('\n')
        wrapped_paragraphs = []
        
        for paragraph in paragraphs:
            if not paragraph.strip():
                wrapped_paragraphs.append('')
                continue
                
            # Split paragraph into words
            words = paragraph.split()
            if not words:
                wrapped_paragraphs.append('')
                continue
            
            lines = []
            current_line = ""
            
            for word in words:
                # Handle long words that exceed line length (CSS word-break + hyphens behavior)
                if len(word) > max_chars_per_line:
                    # Finish current line if it has content
                    if current_line:
                        lines.append(current_line)
                        current_line = ""
                    
                    # Break long word with smart hyphenation (mimic CSS hyphens: auto)
                    remaining_word = word
                    while len(remaining_word) > max_chars_per_line:
                        # CSS hyphens: auto tries to break at syllable boundaries
                        # Simple heuristic: try to break after vowels when possible
                        break_point = max_chars_per_line - 1  # Reserve space for hyphen
                        
                        # Look for better break points in the last few characters
                        vowels = 'aeiouAEIOU'
                        consonants = 'bcdfghjklmnpqrstvwxyzBCDFGHJKLMNPQRSTVWXYZ'
                        
                        # Try to find vowel-consonant boundary for more natural breaks
                        for i in range(max(1, break_point - 4), break_point):
                            if (i < len(remaining_word) - 1 and 
                                remaining_word[i] in vowels and 
                                remaining_word[i + 1] in consonants):
                                break_point = i + 1
                                break
                        
                        # Ensure minimum word chunk size
                        break_point = max(2, break_point)
                        
                        chunk = remaining_word[:break_point] + "-"
                        lines.append(chunk)
                        remaining_word = remaining_word[break_point:]
                        print(f"🔪 CSS-style hyphenation: '{chunk}' | remaining: '{remaining_word}'")
                    
                    # Start new line with remaining word part
                    current_line = remaining_word
                else:
                    # Normal word - check if it fits on current line
                    test_line = current_line + " " + word if current_line else word
                    
                    if len(test_line) <= max_chars_per_line:
                        current_line = test_line
                    else:
                        # Start new line
                        if current_line:
                            lines.append(current_line)
                        current_line = word
            
            # Add the final line of this paragraph
            if current_line:
                lines.append(current_line)
            
            wrapped_paragraphs.extend(lines)
        
        wrapped_result = "\n".join(wrapped_paragraphs)
        print(f"Text wrapping result: '{text}' -> '{wrapped_result}'")
        
        return wrapped_result
        
    except Exception as e:
        print(f"Error in text wrapping: {e}")
        return text  # Return original text if wrapping fails

def add_text_overlay_to_image(img, text_elem):
    """Add text overlay to PIL image using PicTex for exact frontend styling"""
    print(f"=== STARTING TEXT OVERLAY PROCESSING ===")
    
    # Get text element data first
    text = text_elem.get('text', '')
    if not text.strip():
        print(f"Skipping empty text element")
        return
        
    # Get positioning and styling from frontend
    position = text_elem.get('position', {'x': 0, 'y': 0})
    font_size = text_elem.get('font_size', 18)  # Frontend default is 18px
    font_color = text_elem.get('font_color', '#FFFFFF')
    width = text_elem.get('width', 300)  # Frontend default width
    height = text_elem.get('height', 80)  # Frontend default height
    text_align = text_elem.get('text_align', 'center')
    has_stroke = text_elem.get('has_stroke', False)  # Stroke/border option
    
    # If has_stroke is enabled, use black fill with white stroke
    if has_stroke:
        font_color = '#000000'  # Black fill color
        stroke_color = '#FFFFFF'  # White stroke
    else:
        stroke_color = None
    
    print(f"Processing text element: '{text}' at position ({position['x']}, {position['y']}) with font_size {font_size}")
    print(f"Text element dimensions: {width}x{height}, color: {font_color}, align: {text_align}, has_stroke: {has_stroke}")
    print(f"Image dimensions: {img.width}x{img.height}")
    
    # DETAILED LOGGING: Compare frontend vs backend positioning
    print(f"=== TEXT POSITIONING DEBUG ===")
    print(f"Frontend coordinates: x={position['x']}, y={position['y']}")
    print(f"Frontend container size: {width}x{height}")
    print(f"Frontend font size: {font_size}")
    print(f"Frontend text: '{text}'")
    print(f"================================")
    
    # Check if we can import PicTex
    try:
        from pictex import Canvas, FontWeight, TextAlign, Shadow
        print("✅ PicTex import successful - using PicTex rendering")
        pictex_available = True
    except ImportError as import_error:
        print(f"❌ PicTex import failed: {import_error}")
        print("⚠️  Will use PIL fallback only")
        pictex_available = False
    
    if pictex_available:
        # Try PicTex rendering
        try:
            import tempfile
            import os
            
            # Frontend uses 384x480 container, and we're outputting 384x480 (EXACT SAME SIZE)
            # NO SCALING NEEDED - use coordinates and dimensions directly from frontend
            frontend_width, frontend_height = 384, 480
            output_width, output_height = 384, 480
            scale_x = output_width / frontend_width    # 384/384 = 1.0 (no scaling)
            scale_y = output_height / frontend_height  # 480/480 = 1.0 (no scaling)
            
            # Use positioning and dimensions EXACTLY as frontend (1:1 mapping)
            scaled_x = int(position['x'] * scale_x)  # Same as position['x']
            scaled_y = int(position['y'] * scale_y)  # Same as position['y']
            scaled_width = int(width * scale_x)      # Same as width
            scaled_height = int(height * scale_y)    # Same as height
            scaled_font_size = int(font_size * min(scale_x, scale_y))  # Same as font_size
            
            print(f"Scaling factors: x={scale_x:.2f}, y={scale_y:.2f}")
            print(f"Scaled position: ({scaled_x}, {scaled_y}), scaled font size: {scaled_font_size}")
            
            # Convert alignment to PicTex format
            if text_align == 'center':
                alignment = TextAlign.CENTER
            elif text_align == 'right':
                alignment = TextAlign.RIGHT
            else:
                alignment = TextAlign.LEFT
            
            # Calculate max width for text wrapping: match frontend (no internal padding)
            padding = 0
            max_text_width = scaled_width
            
            print(f"Text wrapping calculation: container_width={scaled_width}, padding={padding}, max_text_width={max_text_width}")
            
            # CRITICAL: Implement manual text wrapping since PicTex doesn't have .width() method
            # We need to wrap the text to fit within max_text_width before rendering
            wrapped_text = wrap_text_to_width(text, scaled_font_size, max_text_width)
            print(f"Original text: '{text}'")
            print(f"Wrapped text: '{wrapped_text}'")
            
            # Create PicTex canvas with exact frontend styling
            # Frontend CSS: font-bold, line-height: 1.2, 4-direction text shadow
            # Note: PicTex doesn't have a .width() method - we'll handle text wrapping manually
            # Determine shadow/stroke color based on has_stroke option
            shadow_color = "white" if has_stroke else "black"
            # Use thicker stroke offset for stroke effect
            stroke_offset = 2 if has_stroke else 1
            
            canvas = (
                Canvas()
                .font_family(os.path.join(os.path.dirname(__file__), 'fonts', 'Inter-700.ttf'))
                .font_size(scaled_font_size)
                .font_weight(FontWeight.BOLD)  # Matches frontend font-bold class
                .color(font_color)
                .text_align(alignment)
                .line_height(1.4)  # Increased from 1.2 for better multi-line spacing in exports
                # Add 4-direction text shadow/stroke - white when has_stroke, black otherwise
                .text_shadows(
                    Shadow(offset=(0, stroke_offset), blur_radius=0, color=shadow_color),
                    Shadow(offset=(0, -stroke_offset), blur_radius=0, color=shadow_color),
                    Shadow(offset=(stroke_offset, 0), blur_radius=0, color=shadow_color),
                    Shadow(offset=(-stroke_offset, 0), blur_radius=0, color=shadow_color),
                    # Add diagonal shadows for better stroke coverage when has_stroke
                    *([Shadow(offset=(stroke_offset, stroke_offset), blur_radius=0, color=shadow_color),
                       Shadow(offset=(-stroke_offset, stroke_offset), blur_radius=0, color=shadow_color),
                       Shadow(offset=(stroke_offset, -stroke_offset), blur_radius=0, color=shadow_color),
                       Shadow(offset=(-stroke_offset, -stroke_offset), blur_radius=0, color=shadow_color)] if has_stroke else [])
                )
            )
            
            print(f"PicTex canvas configured for text wrapping within {max_text_width}px")
            
            # Render wrapped text with PicTex
            text_image = canvas.render(wrapped_text)
            
            # Convert PicTex image to PIL
            pictex_pil = text_image.to_pillow()
            
            # Position within container to match frontend (flex center by default)
            text_img_width, text_img_height = pictex_pil.size

            available_width = scaled_width
            available_height = scaled_height

            if text_align == 'left':
                paste_x = scaled_x
            elif text_align == 'right':
                paste_x = scaled_x + max(0, available_width - text_img_width)
            else:
                paste_x = scaled_x + max(0, (available_width - text_img_width) // 2)
            
            # CRITICAL FIX: When rendered text is taller than container, position from top
            # instead of trying to center (which would push text off screen)
            # This matches CSS behavior where overflow text flows downward
            if text_img_height > available_height:
                # Text is too tall for container - position at top of container
                paste_y = scaled_y
                print(f"⚠️  Text overflow detected: rendered height {text_img_height}px > container height {available_height}px")
                print(f"    Positioning text at top of container (y={paste_y}) instead of centering")
            else:
                # Text fits - center vertically within container
                paste_y = scaled_y + (available_height - text_img_height) // 2
            
            # CRITICAL DEBUGGING: Log exactly what we're doing
            print(f"=== FINAL POSITIONING CALCULATION (FRONTEND MATCHING) ===")
            print(f"Container: x={scaled_x}, y={scaled_y}, w={scaled_width}, h={scaled_height}")
            print(f"Rendered text size: {text_img_width}x{text_img_height}")
            print(f"Available space: {available_width}x{available_height}")
            print(f"Text positioning X based on align '{text_align}': {paste_x}")
            print(f"Text positioning Y (vertical center): {paste_y}")
            print(f"Final paste position: ({paste_x}, {paste_y})")
            print(f"===========================================================")
            
            # Boundary safety check - ensure text doesn't go outside image bounds
            paste_x = max(0, min(paste_x, img.width - text_img_width))
            paste_y = max(0, min(paste_y, img.height - text_img_height))
            
            if paste_x != scaled_x + padding + max(0, (available_width - text_img_width) // 2) or \
               paste_y != scaled_y + padding + max(0, (available_height - text_img_height) // 2):
                print(f"⚠️  Applied boundary correction: final position ({paste_x}, {paste_y})")
            
            # Paste the text image onto the main image
            if pictex_pil.mode == 'RGBA':
                # Use alpha compositing for proper transparency
                img.paste(pictex_pil, (paste_x, paste_y), pictex_pil)
            else:
                # Convert to RGBA if needed
                pictex_pil = pictex_pil.convert('RGBA')
                img.paste(pictex_pil, (paste_x, paste_y), pictex_pil)
            
            print(f"Successfully added PicTex text overlay: '{text}' at position ({paste_x}, {paste_y})")
            print(f"=== PICTEX SUCCESS - TEXT OVERLAY COMPLETE ===")
            return  # Success - don't run fallback
            
        except Exception as e:
            print(f"=== PICTEX FAILED - SWITCHING TO FALLBACK ===")
            print(f"Error adding PicTex text overlay: {e}")
            import traceback
            traceback.print_exc()
            pictex_available = False  # Force fallback
    
    # Use PIL fallback if PicTex is not available or failed
    print("=== STARTING PIL FALLBACK RENDERING ===")
    print("Using PIL fallback text rendering...")
    try:
        from PIL import ImageFont, ImageDraw
        
        # Create drawing context for fallback
        draw = ImageDraw.Draw(img)
        
        # Recalculate scaling factors for fallback - output is 384x480 (SAME AS PREVIEW)
        frontend_width, frontend_height = 384, 480
        output_width, output_height = 384, 480
        fallback_scale_x = output_width / frontend_width  # 384/384 = 1.0 (no scaling)
        fallback_scale_y = output_height / frontend_height  # 480/480 = 1.0 (no scaling)
        
        # Scale positioning and dimensions for fallback
        scaled_x = int(position['x'] * fallback_scale_x)
        scaled_y = int(position['y'] * fallback_scale_y)
        scaled_width = int(width * fallback_scale_x)
        scaled_height = int(height * fallback_scale_y)
        
        # Basic fallback using PIL - get font_size from text_elem
        fallback_font_size = text_elem.get('font_size', 18)
        scaled_font_size = int(fallback_font_size * min(fallback_scale_x, fallback_scale_y))
        
        # Convert hex color to RGB - use black if has_stroke
        if has_stroke:
            rgb_color = (0, 0, 0)  # Black fill
            outline_color = (255, 255, 255)  # White stroke
        else:
            fallback_font_color = text_elem.get('font_color', '#FFFFFF')
            if fallback_font_color.startswith('#'):
                fallback_font_color = fallback_font_color[1:]
            rgb_color = tuple(int(fallback_font_color[i:i+2], 16) for i in (0, 2, 4))
            outline_color = (0, 0, 0)  # Black outline
        
        # Try to load default font
        try:
            font = ImageFont.load_default()
        except:
            print("Warning: Could not load any font")
            return
        
        # Apply text wrapping with no internal padding to match frontend
        padding = 0
        max_text_width = scaled_width
        
        print(f"PIL fallback text wrapping: container_width={scaled_width}, padding={padding}, max_text_width={max_text_width}")
        
        # Wrap text to fit within container
        wrapped_text = wrap_text_to_width(text, scaled_font_size, max_text_width)
        print(f"PIL fallback wrapped text: '{wrapped_text}'")
        
        # Simple text rendering at center of text area matching frontend behavior using wrapped text
        text_bbox = draw.textbbox((0, 0), wrapped_text, font=font)
        text_width = text_bbox[2] - text_bbox[0]
        text_height = text_bbox[3] - text_bbox[1]
        
        # Align within the container area based on text_align
        available_width = scaled_width
        available_height = scaled_height

        if text_align == 'left':
            text_x = scaled_x
        elif text_align == 'right':
            text_x = scaled_x + max(0, available_width - text_width)
        else:
            text_x = scaled_x + max(0, (available_width - text_width) // 2)
        
        # CRITICAL FIX: When rendered text is taller than container, position from top
        if text_height > available_height:
            text_y = scaled_y
            print(f"⚠️  PIL fallback text overflow: {text_height}px > {available_height}px, positioning at top")
        else:
            text_y = scaled_y + (available_height - text_height) // 2
        
        # Apply boundary constraints to prevent text overflow beyond image boundaries
        text_x = max(5, min(text_x, img.width - text_width - 5))  # 5px safety margin
        text_y = max(5, min(text_y, img.height - text_height - 5))  # 5px safety margin
        
        # Draw outline/stroke then fill text
        stroke_offsets = [(0, 1), (0, -1), (1, 0), (-1, 0)]
        if has_stroke:
            # Add diagonal offsets for thicker stroke effect
            stroke_offsets.extend([(1, 1), (-1, 1), (1, -1), (-1, -1)])
        for dx, dy in stroke_offsets:
            draw.text((text_x + dx, text_y + dy), wrapped_text, font=font, fill=outline_color)
        draw.text((text_x, text_y), wrapped_text, font=font, fill=rgb_color)
        
        print(f"Fallback: Drew text at ({text_x}, {text_y}) centered in container with {padding}px padding, has_stroke={has_stroke}")
        print(f"=== PIL FALLBACK COMPLETE ===")
        
    except Exception as fallback_error:
        print(f"=== BOTH PICTEX AND PIL FAILED ===")
        print(f"Fallback rendering also failed: {fallback_error}")

def add_text_overlay_to_image_hires(img_hires, text_elem, scale_factor):
    """Add text overlay to high-resolution PIL image for crisp quality"""
    print(f"=== STARTING HIGH-RES TEXT OVERLAY PROCESSING (Scale: {scale_factor}x) ===")
    
    # Get text element data first
    text = text_elem.get('text', '')
    if not text.strip():
        print(f"Skipping empty text element")
        return
        
    # Get positioning and styling from frontend - scale everything by scale_factor
    position = text_elem.get('position', {'x': 0, 'y': 0})
    font_size = text_elem.get('font_size', 18)  # Frontend default is 18px
    font_color = text_elem.get('font_color', '#FFFFFF')
    width = text_elem.get('width', 300)  # Frontend default width
    height = text_elem.get('height', 80)  # Frontend default height
    text_align = text_elem.get('text_align', 'center')
    has_stroke = text_elem.get('has_stroke', False)  # Stroke/border option
    
    # If has_stroke is enabled, use black fill with white stroke
    if has_stroke:
        font_color = '#000000'  # Black fill color
    
    # SCALE ALL DIMENSIONS by scale_factor for high-resolution rendering
    scaled_x = int(position['x'] * scale_factor)
    scaled_y = int(position['y'] * scale_factor)
    scaled_width = int(width * scale_factor)
    scaled_height = int(height * scale_factor)
    scaled_font_size = int(font_size * scale_factor)
    
    print(f"High-res text element: '{text}' at ({scaled_x}, {scaled_y})")
    print(f"High-res dimensions: {scaled_width}x{scaled_height}, font_size: {scaled_font_size}")
    print(f"High-res image dimensions: {img_hires.width}x{img_hires.height}")
    
    # Check if we can import PicTex
    try:
        from pictex import Canvas, FontWeight, TextAlign, Shadow
        print("✅ PicTex import successful - using HIGH-RES PicTex rendering")
        pictex_available = True
    except ImportError as import_error:
        print(f"❌ PicTex import failed: {import_error}")
        print("⚠️  Will use HIGH-RES PIL fallback only")
        pictex_available = False
    
    if pictex_available:
        # Try high-resolution PicTex rendering
        try:
            print(f"=== HIGH-RESOLUTION PICTEX RENDERING ===")
            
            # Convert alignment to PicTex format
            if text_align == 'center':
                alignment = TextAlign.CENTER
            elif text_align == 'right':
                alignment = TextAlign.RIGHT
            else:
                alignment = TextAlign.LEFT
            
            # CRITICAL FIX: Calculate max width for text wrapping at high resolution
            # Text should wrap to fit within the container width minus padding to EXACTLY match frontend
            # Frontend uses p-1 (4px) padding on text container, so we use 4px * scale_factor
            padding = 4 * scale_factor  # Match frontend's p-1 padding (4px at normal scale)
            max_text_width = scaled_width - (2 * padding)  # Use container width minus padding for wrapping
            
            print(f"High-res text wrapping: container_width={scaled_width}, padding={padding}, max_text_width={max_text_width}")
            
            # Wrap text using high-resolution metrics
            wrapped_text = wrap_text_to_width(text, scaled_font_size, max_text_width)
            print(f"High-res wrapped text: '{wrapped_text}'")
            
            # Determine shadow/stroke color based on has_stroke option
            shadow_color = "white" if has_stroke else "black"
            # Use thicker stroke offset for stroke effect (scaled)
            stroke_offset = int(scale_factor * 2) if has_stroke else scale_factor
            
            # Create high-resolution PicTex canvas
            canvas = (
                Canvas()
                .font_family(os.path.join(os.path.dirname(__file__), 'fonts', 'Inter-700.ttf'))
                .font_size(scaled_font_size)  # High-res font size
                .font_weight(FontWeight.BOLD)
                .color(font_color)
                .text_align(alignment)
                .line_height(1.4)  # Increased from 1.2 for better multi-line spacing in exports
                # Add 4-direction text shadow/stroke at high resolution
                .text_shadows(
                    Shadow(offset=(0, stroke_offset), blur_radius=0, color=shadow_color),
                    Shadow(offset=(0, -stroke_offset), blur_radius=0, color=shadow_color),
                    Shadow(offset=(stroke_offset, 0), blur_radius=0, color=shadow_color),
                    Shadow(offset=(-stroke_offset, 0), blur_radius=0, color=shadow_color),
                    # Add diagonal shadows for better stroke coverage when has_stroke
                    *([Shadow(offset=(stroke_offset, stroke_offset), blur_radius=0, color=shadow_color),
                       Shadow(offset=(-stroke_offset, stroke_offset), blur_radius=0, color=shadow_color),
                       Shadow(offset=(stroke_offset, -stroke_offset), blur_radius=0, color=shadow_color),
                       Shadow(offset=(-stroke_offset, -stroke_offset), blur_radius=0, color=shadow_color)] if has_stroke else [])
                )
            )
            
            print(f"High-res PicTex canvas configured with font size {scaled_font_size} and {scale_factor}px shadows")
            
            # Render wrapped text at high resolution
            text_image = canvas.render(wrapped_text)
            pictex_pil = text_image.to_pillow()
            
            # Calculate high-resolution positioning
            text_img_width, text_img_height = pictex_pil.size
            
            # Use same padding as text wrapping to match frontend's p-1 (4px at normal scale)
            padding = 4 * scale_factor  # Match frontend's p-1 padding
            available_width = scaled_width - (2 * padding)
            available_height = scaled_height - (2 * padding)
            
            # Center the text within the container with natural padding
            paste_x = scaled_x + padding + max(0, (available_width - text_img_width) // 2)
            
            # CRITICAL FIX: When rendered text is taller than container, position from top
            # instead of trying to center (which would push text off screen)
            if text_img_height > available_height:
                paste_y = scaled_y + padding  # Position at top with padding
                print(f"⚠️  High-res text overflow: {text_img_height}px > {available_height}px, positioning at top")
            else:
                paste_y = scaled_y + padding + (available_height - text_img_height) // 2
            
            print(f"=== HIGH-RES POSITIONING CALCULATION ===")
            print(f"High-res container: x={scaled_x}, y={scaled_y}, w={scaled_width}, h={scaled_height}")
            print(f"High-res padding: {padding}px (matching frontend p-1: 4px * {scale_factor}x scale)")
            print(f"High-res text size: {text_img_width}x{text_img_height}")
            print(f"High-res paste position: ({paste_x}, {paste_y})")
            print(f"========================================")
            
            # Boundary safety check for high-res image
            paste_x = max(0, min(paste_x, img_hires.width - text_img_width))
            paste_y = max(0, min(paste_y, img_hires.height - text_img_height))
            
            # Paste high-res text onto high-res image
            if pictex_pil.mode == 'RGBA':
                img_hires.paste(pictex_pil, (paste_x, paste_y), pictex_pil)
            else:
                img_hires.paste(pictex_pil, (paste_x, paste_y))
            
            print(f"Successfully added HIGH-RES PicTex text overlay at ({paste_x}, {paste_y})")
            print(f"=== HIGH-RES PICTEX SUCCESS ===")
            return  # Success - don't run fallback
            
        except Exception as e:
            print(f"=== HIGH-RES PICTEX FAILED - SWITCHING TO FALLBACK ===")
            print(f"Error adding high-res PicTex text: {e}")
            pictex_available = False  # Force fallback
    
    # High-resolution PIL fallback
    print("=== STARTING HIGH-RES PIL FALLBACK RENDERING ===")
    try:
        from PIL import ImageFont, ImageDraw
        
        draw = ImageDraw.Draw(img_hires)
        
        # Convert hex color to RGB - use black if has_stroke
        if has_stroke:
            rgb_color = (0, 0, 0)  # Black fill
            outline_color = (255, 255, 255)  # White stroke
        else:
            fallback_font_color = text_elem.get('font_color', '#FFFFFF')
            if fallback_font_color.startswith('#'):
                fallback_font_color = fallback_font_color[1:]
            rgb_color = tuple(int(fallback_font_color[i:i+2], 16) for i in (0, 2, 4))
            outline_color = (0, 0, 0)  # Black outline
        
        # Load high-resolution font
        try:
            font = ImageFont.truetype(os.path.join(os.path.dirname(__file__), 'fonts', 'Inter-700.ttf'), scaled_font_size)
        except Exception:
            try:
                font = ImageFont.truetype("Inter.ttf", scaled_font_size)
            except Exception:
                font = ImageFont.load_default()
        
        # CRITICAL FIX: Apply text wrapping in PIL fallback too
        # Calculate same padding and max width as PicTex method
        # Use same padding as frontend's p-1 (4px at normal scale)
        padding = 4 * scale_factor  # Match frontend's p-1 padding
        max_text_width = scaled_width - (2 * padding)  # Use container width minus padding for wrapping
        
        print(f"High-res PIL fallback text wrapping: container_width={scaled_width}, padding={padding}, max_text_width={max_text_width}")
        
        # Wrap text using high-resolution metrics
        wrapped_text = wrap_text_to_width(text, scaled_font_size, max_text_width)
        print(f"High-res PIL fallback wrapped text: '{wrapped_text}'")
        
        # High-res text measurement using wrapped text
        text_bbox = draw.textbbox((0, 0), wrapped_text, font=font)
        text_width = text_bbox[2] - text_bbox[0]
        text_height = text_bbox[3] - text_bbox[1]
        
        # Use same padding as text wrapping to match frontend's p-1 (4px at normal scale)
        available_width = scaled_width - (2 * padding)
        available_height = scaled_height - (2 * padding)
        
        # Center the text within the container with natural padding
        text_x = scaled_x + padding + max(0, (available_width - text_width) // 2)
        
        # CRITICAL FIX: When rendered text is taller than container, position from top
        if text_height > available_height:
            text_y = scaled_y + padding  # Position at top with padding
            print(f"⚠️  High-res PIL text overflow: {text_height}px > {available_height}px, positioning at top")
        else:
            text_y = scaled_y + padding + (available_height - text_height) // 2
        
        # Apply boundary constraints to ensure text doesn't overflow beyond image boundaries
        margin = scale_factor * 2  # Larger safety margin for high-res
        text_x = max(margin, min(text_x, img_hires.width - text_width - margin))
        text_y = max(margin, min(text_y, img_hires.height - text_height - margin))
        
        # Draw high-res text with scaled shadows/stroke using wrapped text
        shadow_offset = int(scale_factor * 2) if has_stroke else scale_factor
        stroke_offsets = [(0, shadow_offset), (0, -shadow_offset), (shadow_offset, 0), (-shadow_offset, 0)]
        if has_stroke:
            # Add diagonal offsets for thicker stroke effect
            stroke_offsets.extend([(shadow_offset, shadow_offset), (-shadow_offset, shadow_offset), 
                                   (shadow_offset, -shadow_offset), (-shadow_offset, -shadow_offset)])
        for dx, dy in stroke_offsets:
            draw.text((text_x + dx, text_y + dy), wrapped_text, font=font, fill=outline_color)
        draw.text((text_x, text_y), wrapped_text, font=font, fill=rgb_color)
        
        print(f"High-res fallback: Drew text at ({text_x}, {text_y}) with {padding}px padding and {shadow_offset}px shadows, has_stroke={has_stroke}")
        print(f"=== HIGH-RES PIL FALLBACK COMPLETE ===")
        
    except Exception as fallback_error:
        print(f"=== HIGH-RES FALLBACK ALSO FAILED ===")
        print(f"High-res fallback error: {fallback_error}")

def upload_image_to_r2_sync(image_path, filename):
    """Upload processed slideshow image to R2 Storage and return public CDN URL."""
    if not BOTO3_AVAILABLE:
        print("Boto3 client not available, skipping R2 upload")
        return None

    try:
        r2_account_id = os.getenv('R2_ACCOUNT_ID')
        r2_access_key_id = os.getenv('R2_ACCESS_KEY_ID')
        r2_secret_access_key = os.getenv('R2_SECRET_ACCESS_KEY')
        r2_custom_domain = os.getenv('R2_CUSTOM_DOMAIN', 'cdn.reelstacks.ai').strip('\'"')
        bucket_name = 'user-slideshows'  # Different bucket for slideshow images

        if not all([r2_account_id, r2_access_key_id, r2_secret_access_key]):
            print("R2 credentials not fully configured, skipping upload.")
            return None

        s3_client = boto3.client(
            's3',
            endpoint_url=f'https://{r2_account_id}.r2.cloudflarestorage.com',
            aws_access_key_id=r2_access_key_id,
            aws_secret_access_key=r2_secret_access_key,
            region_name='auto'
        )

        print(f"Uploading slideshow image to R2: {filename}")
        s3_client.upload_file(
            image_path, 
            bucket_name, 
            filename, 
            ExtraArgs={
                'ContentType': 'image/png',
                'CacheControl': 'public, max-age=31536000, immutable'  # 1 year cache
            }
        )
        
        # Construct the public CDN URL - user-slideshows bucket is routed to /slideshows/ path
        public_url = f"https://{r2_custom_domain}/slideshows/{filename}"
        print(f"Slideshow image uploaded to R2 successfully: {public_url}")
        return public_url

    except Exception as e:
        print(f"Error uploading slideshow image to R2: {e}")
        return None

def update_slideshow_progress(user_id, job_id, completed_slides, total_slides, generated_images):
    """Update slideshow processing progress in database"""
    if not supabase_client:
        return
    
    try:
        result = supabase_client.table('user_generated_slideshows').update({
            'completed_slides': completed_slides,
            'generated_images': generated_images,
            'updated_at': datetime.now().isoformat()
        }).eq('job_id', job_id).eq('user_id', user_id).execute()
        
        if result.data:
            print(f"Updated slideshow progress: {completed_slides}/{total_slides} slides completed")
        
    except Exception as e:
        print(f"Error updating slideshow progress: {e}")

def mark_slideshow_completed(user_id, job_id, generated_images):
    """Mark slideshow processing as completed"""
    if not supabase_client:
        return
    
    try:
        result = supabase_client.table('user_generated_slideshows').update({
            'status': 'completed',
            'generated_images': generated_images,
            'completed_at': datetime.now().isoformat(),
            'updated_at': datetime.now().isoformat()
        }).eq('job_id', job_id).eq('user_id', user_id).execute()
        
        if result.data:
            print(f"Marked slideshow {job_id} as completed with {len(generated_images)} images")
        
    except Exception as e:
        print(f"Error marking slideshow as completed: {e}")

def mark_slideshow_failed(user_id, job_id, error_message):
    """Mark slideshow processing as failed"""
    if not supabase_client:
        return
    
    try:
        result = supabase_client.table('user_generated_slideshows').update({
            'status': 'failed',
            'error_message': error_message,
            'updated_at': datetime.now().isoformat()
        }).eq('job_id', job_id).eq('user_id', user_id).execute()
        
        print(f"Marked slideshow {job_id} as failed: {error_message}")
        
    except Exception as e:
        print(f"Error marking slideshow as failed: {e}")

if __name__ == '__main__':
    # For development testing
    celery_app.start()