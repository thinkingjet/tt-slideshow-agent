import os
import cv2
import numpy as np
import tempfile
import json
import requests
from typing import Optional
from pathlib import Path
import time
import subprocess
import random
import shutil

# Import Supabase integration for production
try:
    from supabase import create_client, Client
    SUPABASE_AVAILABLE = True
except ImportError:
    print("Warning: Supabase client not available. Install with: pip install supabase")
    SUPABASE_AVAILABLE = False

# Import moviepy components
try:
    from moviepy.editor import VideoFileClip, AudioFileClip, concatenate_videoclips, concatenate_audioclips
    MOVIEPY_AVAILABLE = True
    print("MoviePy loaded successfully for hook+demo processing")
except ImportError as e:
    print(f"Warning: MoviePy not available: {e}")
    MOVIEPY_AVAILABLE = False

# Initialize Supabase client (for production)
supabase_client = None
if SUPABASE_AVAILABLE:
    supabase_url = os.getenv('NEXT_PUBLIC_SUPABASE_URL')
    supabase_key = os.getenv('SUPABASE_SERVICE_ROLE_KEY')
    
    if supabase_url and supabase_key:
        try:
            supabase_client = create_client(supabase_url, supabase_key)
            print("Supabase client initialized for hook+demo video processing")
        except Exception as e:
            print(f"Failed to initialize Supabase client: {e}")

# Check if we're in production mode
IS_PRODUCTION = os.getenv('NODE_ENV') == 'production' or os.getenv('ENVIRONMENT') == 'production'

def download_video_from_supabase(video_url: str, filename: str) -> str:
    """Download video from Supabase URL to temporary file"""
    try:
        print(f"Downloading video from: {video_url}")
        response = requests.get(video_url, stream=True)
        response.raise_for_status()
        
        # Create temporary file
        temp_dir = tempfile.mkdtemp()
        temp_path = os.path.join(temp_dir, filename)
        
        with open(temp_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        
        print(f"Video downloaded to: {temp_path}")
        return temp_path
        
    except Exception as e:
        print(f"Error downloading video: {e}")
        raise

def get_music_path(music_name: str) -> Optional[str]:
    """Get the path to a music file from the backend/music directory"""
    music_dir = os.path.join(os.path.dirname(__file__), 'music')
    
    # Try exact match first
    music_path = os.path.join(music_dir, f"{music_name}.mp3")
    if os.path.exists(music_path):
        return music_path
    
    # If exact match not found, list available music files for debugging
    if os.path.exists(music_dir):
        available_files = [f for f in os.listdir(music_dir) if f.endswith('.mp3')]
        print(f"Available music files: {available_files}")
        
        # Try to find partial match
        for file in available_files:
            if music_name.lower() in file.lower():
                return os.path.join(music_dir, file)
    
    print(f"Music file not found: {music_name}")
    return None

def _draw_filled_rounded_rect(img, top_left, bottom_right, color, radius):
    """Draw a filled rounded rectangle (anti-aliased) on img."""
    x1, y1 = top_left
    x2, y2 = bottom_right
    radius = max(0, min(radius, (min(x2 - x1, y2 - y1)) // 2))
    # Central rectangles
    cv2.rectangle(img, (x1 + radius, y1), (x2 - radius, y2), color, -1)
    cv2.rectangle(img, (x1, y1 + radius), (x2, y2 - radius), color, -1)
    # Four quarter circles
    cv2.circle(img, (x1 + radius, y1 + radius), radius, color, -1, lineType=cv2.LINE_AA)
    cv2.circle(img, (x2 - radius, y1 + radius), radius, color, -1, lineType=cv2.LINE_AA)
    cv2.circle(img, (x1 + radius, y2 - radius), radius, color, -1, lineType=cv2.LINE_AA)
    cv2.circle(img, (x2 - radius, y2 - radius), radius, color, -1, lineType=cv2.LINE_AA)


def add_text_overlay(frame, text: str, width: int, height: int, position: str = "middle", highlight: bool = False):
    """Add text overlay to a frame matching frontend styling exactly"""
    if not text.strip():
        return frame
    
    # Create a copy of the frame to avoid modifying the original
    frame_copy = frame.copy()
    
    # Clean and normalize the caption text to handle encoding issues
    # Replace problematic characters that OpenCV might not render correctly
    caption = str(text).encode('ascii', 'ignore').decode('ascii')
    
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
    
    if position == "top":
        # Match frontend's top positioning with 5% from top
        start_y = vertical_padding + line_height
    elif position == "bottom":
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

        # Optional highlight rectangle behind text
        if highlight:
            pad_y = int(line_height * 0.25)
            pad_x = int(horizontal_padding * 0.25)
            top_left = (max(0, x_position - pad_x), max(0, y_position - text_height - pad_y))
            bottom_right = (min(width, x_position + text_width + pad_x), min(height, y_position + pad_y))
            # Draw filled rounded white rectangle (pill) behind the text
            corner_radius = int(min(text_height + 2 * pad_y, (bottom_right[1] - top_left[1])) * 0.5)
            _draw_filled_rounded_rect(frame_copy, top_left, bottom_right, (255, 255, 255), corner_radius)

        # EXACT FRONTEND MATCH: text-shadow: rgb(0, 0, 0) 0px 1px 0px, rgb(0, 0, 0) 0px -1px 0px, rgb(0, 0, 0) 1px 0px 0px, rgb(0, 0, 0) -1px 0px 0px
        # These are the EXACT shadow offsets from the frontend CSS
        shadow_offsets = [
            (0, 1),   # 0px 1px 0px
            (0, -1),  # 0px -1px 0px  
            (1, 0),   # 1px 0px 0px
            (-1, 0),  # -1px 0px 0px
        ]
        
        # Draw black shadows unless highlight is enabled (then text is black on white)
        if not highlight:
            for dx, dy in shadow_offsets:
                cv2.putText(frame_copy, line, 
                           (x_position + dx, y_position + dy), 
                           cv2.FONT_HERSHEY_DUPLEX,
                           font_scale, 
                           (0, 0, 0),
                           thickness,
                           cv2.LINE_AA)
        
        # Draw main text: white by default, black when highlighted
        cv2.putText(frame_copy, line, 
                   (x_position, y_position), 
                   cv2.FONT_HERSHEY_DUPLEX,
                   font_scale, 
                   (0, 0, 0) if highlight else (255, 255, 255),
                   thickness, 
                   cv2.LINE_AA)
    
    return frame_copy

def add_caption_to_video_opencv(video_path: str, output_path: str, caption: str, text_position: str = "middle", highlight: bool = False):
    """Add text caption to video using OpenCV"""
    try:
        print(f"Adding caption to video: {video_path}")
        
        # Open video for reading
        cap = cv2.VideoCapture(video_path)
        
        # Get video properties
        fps = int(cap.get(cv2.CAP_PROP_FPS))
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        
        print(f"Video properties: {width}x{height}, {fps}fps, {total_frames} frames")
        
        # Setup video writer with H.264 codec for better compatibility
        fourcc = cv2.VideoWriter_fourcc(*'H264')  # Use H.264 for better compatibility
        out = cv2.VideoWriter(output_path, fourcc, fps, (width, height))
        
        # Verify VideoWriter was created successfully
        if not out.isOpened():
            print(f"Failed to open VideoWriter with H264, trying XVID...")
            fourcc = cv2.VideoWriter_fourcc(*'XVID')
            out = cv2.VideoWriter(output_path, fourcc, fps, (width, height))
            if not out.isOpened():
                print(f"Failed to open VideoWriter with XVID, trying mp4v...")
                fourcc = cv2.VideoWriter_fourcc(*'mp4v')
                out = cv2.VideoWriter(output_path, fourcc, fps, (width, height))
                if not out.isOpened():
                    raise Exception("Failed to create VideoWriter for caption overlay")
        
        # Process each frame
        frame_count = 0
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            
            # Add text overlay to frame
            frame_with_text = add_text_overlay(frame, caption, width, height, text_position, highlight)
            
            # Validate frame before writing (same as green screen processor)
            if frame_with_text is not None and frame_with_text.shape[0] > 0 and frame_with_text.shape[1] > 0:
                out.write(frame_with_text)
            else:
                print(f"Warning: Invalid frame with text at {frame_count}")
                out.write(frame)  # Write original frame as fallback
            
            frame_count += 1
            
            # Yield progress occasionally
            if frame_count % 10 == 0:
                progress = int((frame_count / total_frames) * 100)
                yield progress
        
        cap.release()
        out.release()
        print(f"Caption overlay completed. Processed {frame_count} frames.")
        
    except Exception as e:
        print(f"Error adding caption with OpenCV: {e}")
        raise

def concatenate_videos_with_music(hook_video_path: str, demo_video_path: str, music_path: str, output_path: str):
    """Concatenate hook and demo videos with background music throughout"""
    if not MOVIEPY_AVAILABLE:
        raise Exception("MoviePy not available for video processing")
    
    try:
        print(f"Concatenating videos with music:")
        print(f"  Hook video: {hook_video_path}")
        print(f"  Demo video: {demo_video_path}")
        print(f"  Music: {music_path}")
        
        # Load videos
        hook_video = VideoFileClip(hook_video_path)
        demo_video = VideoFileClip(demo_video_path)
        
        # Concatenate videos
        final_video = concatenate_videoclips([hook_video, demo_video], method="compose")
        total_duration = final_video.duration
        
        # Load and loop music to match video duration
        music = AudioFileClip(music_path)
        
        # Calculate how many times we need to loop the music
        loops_needed = int(np.ceil(total_duration / music.duration))
        
        if loops_needed > 1:
            # Create looped music by concatenating multiple copies
            music_clips = [music] * loops_needed
            looped_music = concatenate_audioclips(music_clips)
            # Trim to exact duration
            looped_music = looped_music.subclip(0, total_duration)
        else:
            # Music is longer than video, just trim it
            looped_music = music.subclip(0, total_duration)
        
        # Set the looped music as the audio for the final video
        final_video_with_music = final_video.set_audio(looped_music)
        
        # Resize to Instagram Reels dimensions (9:16 aspect ratio)
        target_width = 1080
        target_height = 1920
        final_video_resized = final_video_with_music.resize(width=target_width, height=target_height)
        
        # Write output
        final_video_resized.write_videofile(
            output_path,
            codec='libx264',
            audio_codec='aac',
            verbose=False,
            logger=None,
            fps=30
        )
        
        # Clean up
        hook_video.close()
        demo_video.close()
        music.close()
        final_video.close()
        final_video_with_music.close()
        final_video_resized.close()
        if loops_needed > 1:
            looped_music.close()
        
        print(f"Videos concatenated successfully with music: {output_path}")
        
    except Exception as e:
        print(f"Error concatenating videos with music: {e}")
        raise

def upload_video_to_supabase_sync(video_path: str, user_id: str, job_id: str) -> Optional[str]:
    """Upload processed video to Supabase storage"""
    if not supabase_client:
        print("Supabase client not available")
        return None
    
    try:
        # Generate unique filename
        timestamp = int(time.time())
        filename = f"hook_demo_{user_id}_{job_id}_{timestamp}.mp4"
        
        # Upload to Supabase storage
        with open(video_path, 'rb') as f:
            video_data = f.read()
        
        result = supabase_client.storage.from_("videos").upload(
            filename, 
            video_data,
            {"content-type": "video/mp4"}
        )
        
        if result:
            # Get public URL
            public_url = supabase_client.storage.from_("videos").get_public_url(filename)
            print(f"Video uploaded to Supabase: {public_url}")
            return public_url
        else:
            print("Failed to upload video to Supabase")
            return None
            
    except Exception as e:
        print(f"Error uploading video to Supabase: {e}")
        return None

def create_processing_entry_sync(user_id: str, job_id: str, metadata: dict) -> Optional[str]:
    """Create processing entry in Supabase database"""
    if not supabase_client:
        print("Supabase client not available")
        return None
    
    try:
        # Insert processing record
        data = {
            "user_id": user_id,
            "job_id": job_id,
            "video_type": "ugc",
            "status": "processing",
            "metadata": metadata,
            "created_at": time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())
        }
        
        result = supabase_client.table("user_videos").insert(data).execute()
        
        if result.data:
            print(f"Processing entry created: {result.data[0]['id']}")
            return result.data[0]['id']
        else:
            print("Failed to create processing entry")
            return None
            
    except Exception as e:
        print(f"Error creating processing entry: {e}")
        return None

def update_video_entry_with_url_sync(user_id: str, job_id: str, video_url: str):
    """Update video entry with final URL"""
    if not supabase_client:
        print("Supabase client not available")
        return
    
    try:
        # Update the record with video URL and completed status
        result = supabase_client.table("user_videos").update({
            "video_url": video_url,
            "status": "completed",
            "processed_at": time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())
        }).eq("job_id", job_id).eq("user_id", user_id).execute()
        
        if result.data:
            print(f"Video entry updated with URL: {video_url}")
        else:
            print("Failed to update video entry")
            
    except Exception as e:
        print(f"Error updating video entry: {e}")

def cleanup_temp_files(*file_paths):
    """Clean up temporary files and directories"""
    for file_path in file_paths:
        if file_path and os.path.exists(file_path):
            try:
                if os.path.isdir(file_path):
                    import shutil
                    shutil.rmtree(file_path)
                    print(f"Cleaned up temp directory: {file_path}")
                else:
                    os.unlink(file_path)
                    print(f"Cleaned up temp file: {file_path}")
            except Exception as e:
                print(f"Error cleaning up {file_path}: {e}")

def process_hook_demo_video(
    hook_video_path: str,
    demo_video_path: str = None,
    music_path: str = None,
    caption: str = "",
    output_path: str = "",
    text_position: str = "middle",
    highlight_text: bool = False
):
    """
    Process hook+demo video with improved approach:
    1. Validate and repair video files if needed
    2. Add caption to hook video with correct text positioning
    3. If demo video provided: Concatenate hook + demo videos, otherwise use hook only
    4. Remove all audio from video
    5. Add looping music on top (if provided)
    """
    
    print(f"Starting hook+demo processing with text position: {text_position}")
    
    try:
        # Step 1: Validate and repair video files (0-20%)
        print("Step 1: Validating and repairing video files...")
        yield 5
        
        # Create repaired versions of input videos
        temp_dir = os.path.dirname(output_path)
        repaired_hook = os.path.join(temp_dir, "repaired_hook.mp4")
        repaired_demo = os.path.join(temp_dir, "repaired_demo.mp4")
        
        # Repair hook video
        repair_cmd = [
            'ffmpeg', '-y',
            '-i', hook_video_path,
            '-c:v', 'libx264',
            '-preset', 'fast',
            '-crf', '23',
            '-r', '30',
            '-pix_fmt', 'yuv420p',
            '-movflags', '+faststart',
            '-avoid_negative_ts', 'make_zero',
            '-fflags', '+genpts',
            '-max_muxing_queue_size', '1024',
            repaired_hook
        ]
        
        result = subprocess.run(repair_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Hook video repair failed: {result.stderr}")
            # Try with more aggressive repair
            repair_cmd = [
                'ffmpeg', '-y', '-err_detect', 'ignore_err',
                '-i', hook_video_path,
                '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
                '-r', '30', '-pix_fmt', 'yuv420p',
                repaired_hook
            ]
            result = subprocess.run(repair_cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise Exception(f"Failed to repair hook video: {result.stderr}")
        
        yield 10
        
        # Repair demo video (only if provided)
        if demo_video_path and os.path.exists(demo_video_path):
            repair_cmd = [
                'ffmpeg', '-y',
                '-i', demo_video_path,
                '-c:v', 'libx264',
                '-preset', 'fast',
                '-crf', '23',
                '-r', '30',
                '-pix_fmt', 'yuv420p',
                '-movflags', '+faststart',
                '-avoid_negative_ts', 'make_zero',
                '-fflags', '+genpts',
                '-max_muxing_queue_size', '1024',
                repaired_demo
            ]
            
            result = subprocess.run(repair_cmd, capture_output=True, text=True)
            if result.returncode != 0:
                print(f"Demo video repair failed: {result.stderr}")
                # Try with more aggressive repair
                repair_cmd = [
                    'ffmpeg', '-y', '-err_detect', 'ignore_err',
                    '-i', demo_video_path,
                    '-c:v', 'libx264', '-preset', 'fast', '-crf', '23',
                    '-r', '30', '-pix_fmt', 'yuv420p',
                    repaired_demo
                ]
                result = subprocess.run(repair_cmd, capture_output=True, text=True)
                if result.returncode != 0:
                    raise Exception(f"Failed to repair demo video: {result.stderr}")
        else:
            print("No demo video provided - skipping demo video repair")
            repaired_demo = None
        
        yield 20
        
        # Step 2: Add caption to hook video (20-40%)
        print("Step 2: Adding caption to hook video...")
        hook_with_caption = os.path.join(temp_dir, "hook_with_caption.mp4")
        
        if caption and caption.strip():
            # Use OpenCV for text overlay with proper padding/margins (same as green screen processor)
            try:
                print(f"Adding caption with OpenCV: {caption}")
                # Use the existing add_caption_to_video_opencv function with proper padding
                for progress in add_caption_to_video_opencv(repaired_hook, hook_with_caption, caption, text_position, highlight_text):
                    # Map caption progress to 20-40% range
                    mapped_progress = 20 + int(progress * 0.2)
                    yield mapped_progress
                
                print("OpenCV caption overlay completed successfully")
                
                # Re-encode the captioned video to H.264 for better compatibility
                print("Re-encoding captioned video to H.264...")
                temp_h264_video = os.path.join(temp_dir, "hook_h264.mp4")
                reencode_cmd = [
                    'ffmpeg', '-y',
                    '-i', hook_with_caption,
                    '-c:v', 'libx264',
                    '-preset', 'fast',
                    '-crf', '23',
                    '-pix_fmt', 'yuv420p',
                    '-r', '30',
                    temp_h264_video
                ]
                
                result = subprocess.run(reencode_cmd, capture_output=True, text=True)
                if result.returncode == 0:
                    # Replace the original captioned video with H.264 version
                    shutil.move(temp_h264_video, hook_with_caption)
                    print("Successfully re-encoded to H.264")
                else:
                    print(f"Re-encoding failed, using original: {result.stderr}")
                
            except Exception as e:
                print(f"OpenCV text overlay failed: {e}")
                # Use original hook video without caption as fallback
                shutil.copy2(repaired_hook, hook_with_caption)
        else:
            # No caption, use repaired hook video
            shutil.copy2(repaired_hook, hook_with_caption)
        
        yield 40
        
        # Step 3: Concatenate hook + demo videos OR use hook only (40-60%)
        if repaired_demo and os.path.exists(repaired_demo):
            print("Step 3: Concatenating hook and demo videos...")
            combined_video = os.path.join(temp_dir, "combined_video.mp4")
            
            # Create concat file
            concat_file = os.path.join(temp_dir, "concat_list.txt")
            with open(concat_file, 'w') as f:
                f.write(f"file '{hook_with_caption}'\n")
                f.write(f"file '{repaired_demo}'\n")
            
            # Concatenate using concat demuxer
            concat_cmd = [
                'ffmpeg', '-y',
                '-f', 'concat',
                '-safe', '0',
                '-i', concat_file,
                '-c:v', 'libx264',
                '-preset', 'fast',
                '-crf', '23',
                '-r', '30',
                '-pix_fmt', 'yuv420p',
                '-c:a', 'aac',
                combined_video
            ]
            
            result = subprocess.run(concat_cmd, capture_output=True, text=True)
            if result.returncode != 0:
                print(f"Video concatenation failed: {result.stderr}")
                raise Exception(f"Failed to concatenate videos: {result.stderr}")
        else:
            print("Step 3: No demo video - using hook video only...")
            combined_video = hook_with_caption  # Use hook video directly
            concat_file = None  # No concat file needed
        
        yield 60
        
        # Step 4: Remove audio from combined video (60-70%)
        print("Step 4: Removing audio from combined video...")
        silent_video = os.path.join(temp_dir, "silent_video.mp4")
        
        remove_audio_cmd = [
            'ffmpeg', '-y',
            '-i', combined_video,
            '-c:v', 'copy',
            '-an',  # Remove audio
            silent_video
        ]
        
        result = subprocess.run(remove_audio_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Audio removal failed: {result.stderr}")
            raise Exception(f"Failed to remove audio: {result.stderr}")
        
        yield 70
        
        # Step 5: Add looping background music (70-100%)
        if music_path and os.path.exists(music_path):
            print("Step 5: Adding looping background music...")
        else:
            print("Step 5: No background music selected - using silent video...")
        
        if music_path and os.path.exists(music_path):
            # Get video duration
            probe_cmd = [
                'ffprobe', '-v', 'quiet', '-print_format', 'json',
                '-show_format', silent_video
            ]
            result = subprocess.run(probe_cmd, capture_output=True, text=True)
            
            if result.returncode == 0:
                import json
                probe_data = json.loads(result.stdout)
                video_duration = float(probe_data['format']['duration'])
                
                # Add looping music
                final_cmd = [
                    'ffmpeg', '-y',
                    '-i', silent_video,
                    '-stream_loop', '-1',  # Loop audio indefinitely
                    '-i', music_path,
                    '-c:v', 'copy',
                    '-c:a', 'aac',
                    '-shortest',  # Stop when video ends
                    '-map', '0:v:0',  # Video from first input
                    '-map', '1:a:0',  # Audio from second input
                    output_path
                ]
                
                result = subprocess.run(final_cmd, capture_output=True, text=True)
                if result.returncode != 0:
                    print(f"Music addition failed: {result.stderr}")
                    # Fallback: use video without music
                    shutil.copy2(silent_video, output_path)
                else:
                    print("Successfully added looping background music")
            else:
                # Fallback: use video without music
                shutil.copy2(silent_video, output_path)
        else:
            # No music provided, use silent video
            shutil.copy2(silent_video, output_path)
        
        yield 90
        
        # Cleanup temporary files
        temp_files = [repaired_hook, hook_with_caption, silent_video]
        if repaired_demo:
            temp_files.append(repaired_demo)
        if combined_video != hook_with_caption:  # Only remove if it's a separate file
            temp_files.append(combined_video)
        if concat_file:
            temp_files.append(concat_file)
            
        for temp_file in temp_files:
            if temp_file and os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except:
                    pass
        
        yield 100
        print("Hook+demo video processing completed successfully!")
        
    except Exception as e:
        print(f"Error in process_hook_demo_video: {str(e)}")
        # Cleanup on error
        temp_files = [
            os.path.join(temp_dir, "repaired_hook.mp4"),
            os.path.join(temp_dir, "repaired_demo.mp4"),
            os.path.join(temp_dir, "hook_with_caption.mp4"),
            os.path.join(temp_dir, "combined_video.mp4"),
            os.path.join(temp_dir, "silent_video.mp4"),
            os.path.join(temp_dir, "concat_list.txt")
        ]
        for temp_file in temp_files:
            if temp_file and os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except:
                    pass
        raise e

def download_video_from_url(url: str, output_path: str) -> str:
    """Download video from URL to local path"""
    print(f"Downloading video from: {url}")
    
    try:
        response = requests.get(url, stream=True)
        response.raise_for_status()
        
        with open(output_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        
        print(f"Video downloaded to: {output_path}")
        return output_path
        
    except Exception as e:
        print(f"Error downloading video: {e}")
        raise

def get_random_music_file() -> Optional[str]:
    """Get a random music file from the music directory"""
    music_dir = Path(__file__).parent / "music"
    
    if not music_dir.exists():
        print("Music directory not found")
        return None
    
    music_files = list(music_dir.glob("*.mp3")) + list(music_dir.glob("*.wav")) + list(music_dir.glob("*.m4a"))
    
    if not music_files:
        print("No music files found")
        return None
    
    selected_music = random.choice(music_files)
    print(f"Selected music file: {selected_music}")
    return str(selected_music)

def upload_video_to_supabase(video_path: str, user_id: str, job_id: str) -> str:
    """Upload processed video to Supabase storage and return public URL"""
    try:
        if not supabase_client:
            raise Exception("Supabase client not available")
        
        # Generate unique filename like green screen memes
        timestamp = int(time.time())
        filename = f"{user_id}/{timestamp}_{job_id}_final.mp4"
        
        # Read video file
        with open(video_path, 'rb') as f:
            video_data = f.read()
        
        # Upload to Supabase storage using the same method as green screen memes
        print(f"Uploading video to Supabase: {filename}")
        result = supabase_client.storage.from_("user-videos").upload(
            filename, 
            video_data,
            file_options={"content-type": "video/mp4"}
        )
        
        # Get public URL (same method as green screen memes)
        public_url_response = supabase_client.storage.from_("user-videos").get_public_url(filename)
        
        if public_url_response:
            # Handle both string and dict responses from get_public_url
            if isinstance(public_url_response, dict):
                public_url = public_url_response.get('publicUrl', '')
            else:
                public_url = str(public_url_response)
                
            print(f"Video uploaded successfully: {public_url}")
            return public_url
        else:
            print("Failed to get public URL")
            raise Exception("Failed to get public URL")
            
    except Exception as e:
        print(f"Error uploading to Supabase: {e}")
        raise

def cleanup_temp_files(*file_paths):
    """Clean up temporary files and directories"""
    for file_path in file_paths:
        try:
            if os.path.isfile(file_path):
                os.remove(file_path)
                print(f"Cleaned up temp file: {file_path}")
            elif os.path.isdir(file_path):
                shutil.rmtree(file_path)
                print(f"Cleaned up temp directory: {file_path}")
        except Exception as e:
            print(f"Error cleaning up {file_path}: {e}") 