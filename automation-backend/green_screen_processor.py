from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import cv2
import numpy as np
import os
import tempfile
import json
import requests
from typing import Optional
import asyncio
from pathlib import Path

# Supabase integration for production
try:
    from supabase import create_client, Client
    import time
    SUPABASE_AVAILABLE = True
except ImportError:
    print("Warning: Supabase client not available. Install with: pip install supabase")
    SUPABASE_AVAILABLE = False

# Import moviepy components
try:
    from moviepy.editor import VideoFileClip, TextClip, CompositeVideoClip
    MOVIEPY_AVAILABLE = True
except ImportError as e:
    print(f"Warning: MoviePy not available: {e}")
    MOVIEPY_AVAILABLE = False

app = FastAPI()

# Initialize Supabase client (for production)
supabase_client = None
if SUPABASE_AVAILABLE:
    supabase_url = os.getenv('NEXT_PUBLIC_SUPABASE_URL')
    supabase_key = os.getenv('SUPABASE_SERVICE_ROLE_KEY')  # Use service role key for backend
    
    if supabase_url and supabase_key:
        try:
            supabase_client = create_client(supabase_url, supabase_key)
            print("Supabase client initialized for production video uploads")
        except Exception as e:
            print(f"Failed to initialize Supabase client: {e}")

# Check if we're in production mode
IS_PRODUCTION = os.getenv('NODE_ENV') == 'production' or os.getenv('ENVIRONMENT') == 'production'

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if IS_PRODUCTION else ["http://localhost:3000", "http://localhost:3001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ProcessingRequest(BaseModel):
    jobId: str
    videoFilename: str
    caption: str
    hasBackgroundImage: bool
    backgroundImagePath: Optional[str] = None
    defaultBackgroundUrl: Optional[str] = None
    userBackgroundUrl: Optional[str] = None
    textPosition: Optional[str] = "middle"
    userId: Optional[str] = None  # For production Supabase uploads

class ProcessingUpdate(BaseModel):
    status: str
    progress: int
    message: str
    result_url: Optional[str] = None

def remove_green_screen(video_path: str, output_path: str, background_image_path: Optional[str] = None, user_background_url: Optional[str] = None, default_background_url: Optional[str] = None):
    """Remove green screen from video and optionally add background"""
    print(f"Starting green screen removal: {video_path}")
    cap = cv2.VideoCapture(video_path)
    
    # Get original video properties - use exact same FPS to avoid timing issues
    fps = cap.get(cv2.CAP_PROP_FPS)
    original_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    original_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    
    print(f"Original video properties: {original_width}x{original_height}, {fps}fps, {total_frames} frames")
    
    # Instagram Reels dimensions (9:16 aspect ratio)
    target_width = 1080
    target_height = 1920
    
    # Use EXACTLY the same FPS as input to avoid any timing issues
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    out = cv2.VideoWriter(output_path, fourcc, fps, (target_width, target_height))
    
    # Verify VideoWriter was created successfully
    if not out.isOpened():
        print(f"Failed to open VideoWriter with mp4v, trying XVID...")
        fourcc = cv2.VideoWriter_fourcc(*'XVID')
        out = cv2.VideoWriter(output_path, fourcc, fps, (target_width, target_height))
        if not out.isOpened():
            raise Exception("Failed to create VideoWriter with any codec")
    
    # Load and prepare background image
    background = None
    
    # Priority: 1. Custom uploaded image, 2. User background URL, 3. Default background URL, 4. Fallback gradient
    if background_image_path and os.path.exists(background_image_path):
        background = cv2.imread(background_image_path)
        background = cv2.resize(background, (target_width, target_height))
    elif user_background_url:
        try:
            # Download user background image
            response = requests.get(user_background_url)
            response.raise_for_status()
            
            # Save temporarily
            with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as temp_bg:
                temp_bg.write(response.content)
                temp_bg_path = temp_bg.name
            
            # Load and resize
            background = cv2.imread(temp_bg_path)
            if background is not None:
                background = cv2.resize(background, (target_width, target_height))
            
            # Clean up temp file
            os.unlink(temp_bg_path)
            
        except Exception as e:
            print(f"Error downloading user background: {e}")
            background = None
    elif default_background_url:
        try:
            # If URL is relative (starts with /), try to use local file from public directory
            if default_background_url.startswith('/') and not default_background_url.startswith('//'):
                # Try to get from local public directory
                local_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'public', default_background_url.lstrip('/'))
                if os.path.exists(local_path):
                    print(f"Using local file for default background: {local_path}")
                    background = cv2.imread(local_path)
                    if background is not None:
                        background = cv2.resize(background, (target_width, target_height))
                else:
                    # If local file doesn't exist, try using a base URL
                    base_url = "http://localhost:3000"  # Default for development
                    full_url = base_url + default_background_url
                    print(f"Trying with base URL: {full_url}")
                    response = requests.get(full_url)
                    response.raise_for_status()
                    
                    # Save temporarily
                    with tempfile.NamedTemporaryFile(delete=False, suffix='.png') as temp_bg:
                        temp_bg.write(response.content)
                        temp_bg_path = temp_bg.name
                    
                    # Load and resize
                    background = cv2.imread(temp_bg_path)
                    if background is not None:
                        background = cv2.resize(background, (target_width, target_height))
                    
                    # Clean up temp file
                    os.unlink(temp_bg_path)
            else:
                # Regular URL handling
                response = requests.get(default_background_url)
                response.raise_for_status()
                
                # Save temporarily
                with tempfile.NamedTemporaryFile(delete=False, suffix='.png') as temp_bg:
                    temp_bg.write(response.content)
                    temp_bg_path = temp_bg.name
                
                # Load and resize
                background = cv2.imread(temp_bg_path)
                if background is not None:
                    background = cv2.resize(background, (target_width, target_height))
                
                # Clean up temp file
                os.unlink(temp_bg_path)
            
        except Exception as e:
            print(f"Error downloading default background: {e}")
            background = None
    
    # If no background loaded, create fallback gradient
    if background is None:
        # Create simple gradient background as fallback
        background = np.zeros((target_height, target_width, 3), dtype=np.uint8)
        
        # Simple rose gradient as fallback
        start_color = [245, 180, 180]  # Rose-100
        end_color = [252, 200, 200]    # Rose-200
        
        # Create vertical gradient
        for y in range(target_height):
            ratio = y / target_height
            # Interpolate between start and end colors
            b = int(start_color[0] + ratio * (end_color[0] - start_color[0]))
            g = int(start_color[1] + ratio * (end_color[1] - start_color[1]))
            r = int(start_color[2] + ratio * (end_color[2] - start_color[2]))
            background[y, :] = [b, g, r]
    
    frame_count = 0
    processed_frames = 0
    
    print(f"Starting to process {total_frames} frames...")
    
    # Process frames more efficiently without frequent yielding
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        
        # Convert BGR to HSV for better green screen detection
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        
        # Enhanced green screen detection with multiple ranges
        # Primary green range
        lower_green1 = np.array([35, 40, 40])
        upper_green1 = np.array([85, 255, 255])
        mask1 = cv2.inRange(hsv, lower_green1, upper_green1)
        
        # Secondary green range for different lighting
        lower_green2 = np.array([45, 50, 50])
        upper_green2 = np.array([75, 255, 255])
        mask2 = cv2.inRange(hsv, lower_green2, upper_green2)
        
        # Combine masks
        mask = cv2.bitwise_or(mask1, mask2)
        
        # Morphological operations to clean up the mask
        kernel = np.ones((5, 5), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        
        # Blur the mask to smooth edges
        mask = cv2.GaussianBlur(mask, (5, 5), 0)  # Reduced blur for better performance
        
        # Resize the original frame to fit in the center of the target dimensions
        # Calculate scaling to fit the original video in the target frame while maintaining aspect ratio
        original_aspect = original_width / original_height
        target_aspect = target_width / target_height
        
        if original_aspect > target_aspect:
            # Video is wider - fit by width
            scale = target_width / original_width
            new_width = target_width
            new_height = int(original_height * scale)
        else:
            # Video is taller - fit by height
            scale = target_height / original_height
            new_height = target_height
            new_width = int(original_width * scale)
        
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
        
        # Validate and write result frame immediately for consistent timing
        if result is not None and result.shape[0] > 0 and result.shape[1] > 0:
            out.write(result)
            processed_frames += 1
        else:
            print(f"Warning: Empty or invalid frame at {frame_count}")
            
        frame_count += 1
        
        # Yield progress less frequently to avoid blocking frame processing
        if frame_count % 30 == 0:  # Every 30 frames instead of 10 for smoother processing
            progress = int((frame_count / total_frames) * 50)  # 50% for green screen removal
            yield progress
    
    cap.release()
    out.release()
    print(f"Green screen removal completed. Processed {frame_count} frames.")

def add_text_to_video(video_path: str, output_path: str, caption: str, text_position: str = "middle"):
    """Add text caption to video using OpenCV with exact frame rate preservation"""
    try:
        # Open video for reading
        cap = cv2.VideoCapture(video_path)
        
        # Get video properties - use EXACT same parameters as input
        fps = cap.get(cv2.CAP_PROP_FPS)
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        
        print(f"Text overlay - Input video: {width}x{height}, {fps}fps, {total_frames} frames")
        
        # CRITICAL: Validate FPS to prevent timing issues
        if fps <= 0:
            print(f"Warning: Invalid FPS {fps}, defaulting to 30fps")
            fps = 30.0
        
        # Use EXACTLY the same codec and FPS as input to avoid any timing issues
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        out = cv2.VideoWriter(output_path, fourcc, fps, (width, height))
        
        # Verify VideoWriter was created successfully
        if not out.isOpened():
            print(f"Failed to open VideoWriter with mp4v, trying XVID...")
            fourcc = cv2.VideoWriter_fourcc(*'XVID')
            out = cv2.VideoWriter(output_path, fourcc, fps, (width, height))
            if not out.isOpened():
                raise Exception("Failed to create VideoWriter for text overlay")
        
        print(f"VideoWriter created: {width}x{height}, {fps}fps")
        
        # Process frames with frame counting validation
        frame_count = 0
        frames_written = 0
        
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            
            frame_count += 1
            
            # Add text overlay to frame
            frame_with_text = add_text_overlay(frame, caption, width, height, text_position)
            
            # Validate frame before writing
            if frame_with_text is not None and frame_with_text.shape[:2] == (height, width):
                out.write(frame_with_text)
                frames_written += 1
            else:
                print(f"Warning: Invalid frame at {frame_count}, skipping")
            
        cap.release()
        out.release()
        
        print(f"Text overlay completed: Read {frame_count} frames, wrote {frames_written} frames at {fps}fps")
        
        # CRITICAL: Verify frame count matches to prevent timing issues
        if frame_count != frames_written:
            print(f"ERROR: Frame count mismatch! Read {frame_count}, wrote {frames_written}")
            raise Exception(f"Frame processing error: expected {frame_count} frames, got {frames_written}")
        
        if frame_count != total_frames:
            print(f"WARNING: Processed {frame_count} frames but expected {total_frames}")
        
    except Exception as e:
        print(f"Error in add_text_to_video: {e}")
        # Fallback: copy original video
        import shutil
        shutil.copy2(video_path, output_path)

def add_text_overlay(frame, caption, width, height, text_position="middle"):
    """Add text overlay to a single frame matching frontend styling exactly"""
    
    # Clean and normalize the caption text to handle encoding issues
    # Replace problematic characters that OpenCV might not render correctly
    caption = str(caption).encode('ascii', 'ignore').decode('ascii')
    
    # Replace common Unicode characters with ASCII equivalents
    caption = caption.replace('\u2019', "'")  # Unicode apostrophe to ASCII apostrophe
    caption = caption.replace('\u201c', '"')  # Unicode left double quote
    caption = caption.replace('\u201d', '"')  # Unicode right double quote
    caption = caption.replace('\u2013', '-')  # En dash to hyphen
    caption = caption.replace('\u2014', '-')  # Em dash to hyphen
    
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
    
    # Exact positioning to match frontend positions
    vertical_padding = int(height * 0.05)  # 5% padding from top/bottom to match frontend positioning
    
    if text_position == "top":
        # Match frontend's top positioning with 5% from top
        start_y = vertical_padding + line_height
    elif text_position == "bottom":
        # Match frontend's bottom positioning with 5% from bottom
        start_y = height - vertical_padding - total_text_height + line_height
    else:  # middle (default)
        # Match frontend's vertical centering
        start_y = (height - total_text_height) // 2 + line_height
    
    # Draw each line with EXACT frontend text-shadow match
    for i, line in enumerate(lines):
        y_position = start_y + (i * line_height)
        
        # Center text horizontally within the 95% container, respecting px-6 padding
        (text_width, text_height), _ = cv2.getTextSize(line, cv2.FONT_HERSHEY_DUPLEX, font_scale, thickness)
        x_position = text_start_x + horizontal_padding + (effective_width - text_width) // 2
        
        # EXACT FRONTEND MATCH: text-shadow: rgb(0, 0, 0) 0px 1px 0px, rgb(0, 0, 0) 0px -1px 0px, rgb(0, 0, 0) 1px 0px 0px, rgb(0, 0, 0) -1px 0px 0px
        # These are the EXACT shadow offsets from the frontend CSS
        shadow_offsets = [
            (0, 1),   # 0px 1px 0px
            (0, -1),  # 0px -1px 0px  
            (1, 0),   # 1px 0px 0px
            (-1, 0),  # -1px 0px 0px
        ]
        
        # Draw black shadows with exact CSS positioning
        for dx, dy in shadow_offsets:
            cv2.putText(frame, line, 
                       (x_position + dx, y_position + dy), 
                       cv2.FONT_HERSHEY_DUPLEX,
                       font_scale, 
                       (0, 0, 0),  # Black color rgb(0, 0, 0)
                       thickness,  # Same thickness as main text for clean shadow
                       cv2.LINE_AA)
        
        # EXACT FRONTEND MATCH: text-white - Draw white text on top
        cv2.putText(frame, line, 
                   (x_position, y_position), 
                   cv2.FONT_HERSHEY_DUPLEX,  # Using DUPLEX for better bold appearance
                   font_scale, 
                   (255, 255, 255),  # White color
                   thickness, 
                   cv2.LINE_AA)  # Anti-aliasing for smooth text
    
    return frame

def merge_audio_with_ffmpeg(original_video_path: str, processed_video_path: str, output_path: str):
    """Merge audio from original video with processed video using frame-rate matched encoding"""
    import subprocess
    
    try:
        # First, get the exact frame rate from the original video to ensure perfect sync
        probe_cmd = [
            'ffprobe',
            '-v', 'quiet',
            '-print_format', 'json',
            '-show_streams',
            '-select_streams', 'v:0',
            original_video_path
        ]
        
        probe_result = subprocess.run(probe_cmd, capture_output=True, text=True)
        
        original_fps = None
        if probe_result.returncode == 0:
            import json
            try:
                probe_data = json.loads(probe_result.stdout)
                if 'streams' in probe_data and len(probe_data['streams']) > 0:
                    stream = probe_data['streams'][0]
                    if 'r_frame_rate' in stream:
                        fps_fraction = stream['r_frame_rate']
                        if '/' in fps_fraction:
                            num, den = fps_fraction.split('/')
                            original_fps = float(num) / float(den)
                        else:
                            original_fps = float(fps_fraction)
                        print(f"Original video FPS: {original_fps}")
            except Exception as e:
                print(f"Error parsing probe data: {e}")
        
        # Use frame-rate matched encoding to prevent any timing drift
        cmd = [
            'ffmpeg',
            '-i', processed_video_path,  # Video source (processed video)
            '-i', original_video_path,   # Audio source (original video)
            '-c:v', 'libx264',           # Re-encode video to ensure compatibility
            '-preset', 'medium',         # Good balance of speed and quality
            '-crf', '23',                # Good quality
            '-c:a', 'aac',               # Re-encode audio for compatibility
            '-b:a', '128k',              # Audio bitrate
            '-map', '0:v:0',             # Use video from first input (processed video)
            '-map', '1:a:0',             # Use audio from second input (original video)
            '-shortest',                 # End when shortest stream ends
            '-movflags', '+faststart',   # Optimize for web playback
            '-avoid_negative_ts', 'make_zero',  # Fix timestamp issues
            '-fflags', '+genpts',        # Generate presentation timestamps
        ]
        
        # Force the same frame rate if we detected it
        if original_fps:
            cmd.extend(['-r', str(original_fps)])
        
        cmd.extend(['-y', output_path])  # Overwrite output file
        
        print(f"Running frame-rate matched FFmpeg command: {' '.join(cmd)}")
        
        # Check if input files exist and are readable
        import os
        if not os.path.exists(processed_video_path):
            print(f"ERROR: Processed video file does not exist: {processed_video_path}")
            raise FileNotFoundError(f"Processed video file not found: {processed_video_path}")
        if not os.path.exists(original_video_path):
            print(f"ERROR: Original video file does not exist: {original_video_path}")
            raise FileNotFoundError(f"Original video file not found: {original_video_path}")
        
        print(f"Input files verified - processed: {processed_video_path}, original: {original_video_path}")
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode != 0:
            print(f"FFmpeg failed: {result.stderr}")
            
            # Simple fallback without frame rate forcing
            print("Trying simple fallback...")
            fallback_cmd = [
                'ffmpeg',
                '-i', processed_video_path,
                '-i', original_video_path,
                '-c:v', 'libx264',
                '-preset', 'fast',
                '-crf', '23',
                '-c:a', 'aac',
                '-b:a', '128k',
                '-map', '0:v:0',
                '-map', '1:a:0',
                '-shortest',
                '-avoid_negative_ts', 'make_zero',
                '-fflags', '+genpts',
                '-y',
                output_path
            ]
            
            fallback_result = subprocess.run(fallback_cmd, capture_output=True, text=True)
            
            if fallback_result.returncode != 0:
                print(f"Fallback FFmpeg failed: {fallback_result.stderr}")
                # Final fallback: copy processed video without audio
                import shutil
                shutil.copy2(processed_video_path, output_path)
                print("Copied video without audio as final fallback")
            else:
                print("Fallback FFmpeg succeeded")
        else:
            print("FFmpeg completed successfully")
        
    except Exception as e:
        print(f"Error in merge_audio_with_ffmpeg: {e}")
        # Fallback: copy processed video without audio
        import shutil
        shutil.copy2(processed_video_path, output_path)
        print("Exception occurred, copied video without audio")

async def upload_video_to_supabase(video_path: str, user_id: str, job_id: str) -> Optional[str]:
    """Upload video to Supabase Storage and return public URL"""
    if not supabase_client:
        print("Supabase client not available, skipping upload")
        return None
    
    try:
        # Generate unique filename
        timestamp = int(time.time())
        filename = f"{user_id}/{timestamp}_{job_id}_final.mp4"
        
        # Read video file
        with open(video_path, 'rb') as f:
            video_data = f.read()
        
        # Upload to Supabase Storage
        print(f"Uploading video to Supabase: {filename}")
        result = supabase_client.storage.from_("user-videos").upload(
            filename, 
            video_data,
            file_options={"content-type": "video/mp4"}
        )
        
        if result.get('error'):
            print(f"Supabase upload error: {result['error']}")
            return None
        
        # Get public URL
        public_url_response = supabase_client.storage.from_("user-videos").get_public_url(filename)
        public_url = public_url_response.get('publicUrl')
        
        if public_url:
            print(f"Video uploaded successfully: {public_url}")
            return public_url
        else:
            print("Failed to get public URL")
            return None
            
    except Exception as e:
        print(f"Error uploading to Supabase: {e}")
        return None

async def process_video_stream(request: ProcessingRequest):
    """Process video and stream progress updates"""
    try:
        # Setup paths
        video_dir = Path(__file__).parent.parent / "green_screen_videos"
        video_path = video_dir / request.videoFilename
        
        # Use temp directory in production, public/generated in development
        if IS_PRODUCTION:
            output_dir = Path(tempfile.gettempdir())
        else:
            output_dir = Path(__file__).parent.parent / "public" / "generated"
            output_dir.mkdir(exist_ok=True)
        
        temp_video_path = output_dir / f"{request.jobId}_temp.mp4"
        final_video_path = output_dir / f"{request.jobId}_final.mp4"
        
        # Validate input video exists
        if not video_path.exists():
            yield f"data: {json.dumps({'status': 'error', 'progress': 0, 'message': 'Video file not found'})}\n\n"
            return
        
        # Step 1: Remove green screen
        yield f"data: {json.dumps({'status': 'processing', 'progress': 20, 'message': 'Removing green screen...'})}\n\n"
        
        # Use OpenCV for green screen removal
        for progress in remove_green_screen(
            str(video_path), 
            str(temp_video_path), 
            request.backgroundImagePath,
            request.userBackgroundUrl,
            request.defaultBackgroundUrl
        ):
            yield f"data: {json.dumps({'status': 'processing', 'progress': 20 + progress, 'message': 'Processing green screen...'})}\n\n"
        
        # Step 2: Add text caption and preserve audio
        yield f"data: {json.dumps({'status': 'processing', 'progress': 70, 'message': 'Adding text caption...'})}\n\n"
        
        # Create video with text overlay (no audio yet)
        temp_video_with_text = output_dir / f"{request.jobId}_with_text.mp4"
        add_text_to_video(str(temp_video_path), str(temp_video_with_text), request.caption, request.textPosition or "middle")
        
        # Step 3: Merge audio from original video
        yield f"data: {json.dumps({'status': 'processing', 'progress': 85, 'message': 'Adding audio...'})}\n\n"
        
        merge_audio_with_ffmpeg(str(video_path), str(temp_video_with_text), str(final_video_path))
        
        # Clean up temp files
        if temp_video_path.exists():
            temp_video_path.unlink()
        if temp_video_with_text.exists():
            temp_video_with_text.unlink()
        
        # Step 4: Handle video upload based on environment
        if IS_PRODUCTION and supabase_client and request.userId:
            # Production: Upload to Supabase
            yield f"data: {json.dumps({'status': 'processing', 'progress': 95, 'message': 'Uploading to cloud storage...'})}\n\n"
            
            supabase_url = await upload_video_to_supabase(str(final_video_path), request.userId, request.jobId)
            
            # Clean up local temp file in production
            if final_video_path.exists():
                final_video_path.unlink()
            
            if supabase_url:
                result_url = supabase_url
                yield f"data: {json.dumps({'status': 'completed', 'progress': 100, 'message': 'Video processing complete!', 'result_url': result_url})}\n\n"
            else:
                yield f"data: {json.dumps({'status': 'error', 'progress': 0, 'message': 'Failed to upload video to cloud storage'})}\n\n"
        else:
            # Development: Use local file path
            result_url = f"/generated/{request.jobId}_final.mp4"
            yield f"data: {json.dumps({'status': 'completed', 'progress': 100, 'message': 'Video processing complete!', 'result_url': result_url})}\n\n"
        
    except Exception as e:
        error_message = f"Processing failed: {str(e)}"
        yield f"data: {json.dumps({'status': 'error', 'progress': 0, 'message': error_message})}\n\n"

@app.post("/process-video")
async def process_video(request: ProcessingRequest):
    print(f"Processing video request received: {request}")
    return StreamingResponse(
        process_video_stream(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive"
        }
    )

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "message": "Green screen processing server is running"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
