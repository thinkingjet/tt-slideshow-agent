from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import redis
import json
import asyncio
import os
from typing import Optional
from celery_worker import celery_app, redis_client, check_and_recover_orphaned_jobs, currently_processing_jobs, supabase_client, start_recovery_monitor
from automation_routes import router as automation_router

app = FastAPI(title="Video Processing Queue Server")
app.include_router(automation_router)

# Check if we're in production mode
IS_PRODUCTION = os.getenv('NODE_ENV') == 'production' or os.getenv('ENVIRONMENT') == 'production'

# Initialize orphaned job recovery system (API server only)
ENABLE_RECOVERY = os.getenv('ENABLE_RECOVERY_SYSTEM', 'false').lower() == 'true'

if supabase_client and (IS_PRODUCTION or ENABLE_RECOVERY):
    start_recovery_monitor()
    print("Orphaned job recovery system initialized in API server")
else:
    if not supabase_client:
        print("Orphaned job recovery system disabled (no Supabase client)")
    else:
        print("Orphaned job recovery system disabled (development mode - set ENABLE_RECOVERY_SYSTEM=true to enable)")

# Configure CORS for production
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",  # Local development
        "https://localhost:3000",  # Local development HTTPS
        os.getenv("FRONTEND_URL", "")  # Production frontend URL
    ] if os.getenv("ENVIRONMENT") == "production" else [
        "http://localhost:3000",
        "https://localhost:3000",
        "*"  # Allow all origins in development
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
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
    highlightText: Optional[bool] = False
    userId: Optional[str] = None

class HookDemoRequest(BaseModel):
    jobId: str
    hookVideoUrl: str
    demoVideoUrl: str
    caption: str
    musicName: Optional[str] = ""  # Make music optional with empty string default
    textPosition: Optional[str] = "top"  # Add textPosition field with default
    highlightText: Optional[bool] = False
    userId: str

class AvatarVideoRequest(BaseModel):
    jobId: str
    avatarId: str
    imageUrl: str
    videoPrompt: str
    userId: str

class SlideshowProcessingRequest(BaseModel):
    jobId: str
    userId: str
    slideshowId: str
    slideshowData: dict
    title: str

class JobStatusResponse(BaseModel):
    status: str
    progress: int
    message: str
    result_url: Optional[str] = None
    queue_position: Optional[int] = None

def extract_timestamp_from_job_id(job_id: str) -> int:
    """Extract timestamp from job ID for FIFO priority calculation"""
    try:
        # Job ID format: job_timestamp_randomstring
        parts = job_id.split('_')
        if len(parts) >= 2:
            return int(parts[1])
        return 0
    except (ValueError, IndexError):
        return 0

@app.post("/process-video")
async def process_video(request: ProcessingRequest):
    """Submit video processing job to queue with FIFO priority"""
    try:
        # Extract timestamp from job ID for FIFO ordering
        timestamp = extract_timestamp_from_job_id(request.jobId)
        
        # Calculate priority: earlier timestamps get higher priority (lower number)
        # Use negative timestamp so earlier submissions have lower numbers (higher priority)
        priority = -timestamp if timestamp > 0 else 5
        
        # Submit task to Celery with timestamp-based priority
        task = celery_app.send_task(
            'process_video_async',
            args=[request.dict()],
            task_id=request.jobId,
            priority=priority,
            queue='video_processing'  # Use specific queue for video processing
        )
        
        # Get current queue length for position estimate
        queue_length = redis_client.llen('video_processing')
        
        print(f"Submitted job {request.jobId} with timestamp {timestamp} and priority {priority}")
        
        return {
            "job_id": request.jobId,
            "status": "queued",
            "message": "Video processing job submitted to queue",
            "queue_position": queue_length + 1,
            "priority": priority,
            "timestamp": timestamp
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to submit job: {str(e)}")

@app.post("/process-hook-demo")
async def process_hook_demo(request: HookDemoRequest):
    """Submit hook+demo video processing job to queue with FIFO priority"""
    try:
        # Debug: surface highlight flag received from frontend
        try:
            print(f"HookDemoRequest highlightText={request.highlightText}, textPosition={request.textPosition}")
        except Exception:
            pass
        # Extract timestamp from job ID for FIFO ordering
        timestamp = extract_timestamp_from_job_id(request.jobId)
        
        # Calculate priority: earlier timestamps get higher priority (lower number)
        # Use negative timestamp so earlier submissions have lower numbers (higher priority)
        priority = -timestamp if timestamp > 0 else 5
        
        # Submit task to Celery with timestamp-based priority
        task = celery_app.send_task(
            'process_hook_demo_async',
            args=[request.dict()],
            task_id=request.jobId,
            priority=priority,
            queue='video_processing'  # Use same queue for consistency
        )
        
        # Get current queue length for position estimate
        queue_length = redis_client.llen('video_processing')
        
        print(f"Submitted hook+demo job {request.jobId} with timestamp {timestamp} and priority {priority}")
        
        return {
            "job_id": request.jobId,
            "status": "queued",
            "message": "Hook+demo video processing job submitted to queue",
            "queue_position": queue_length + 1,
            "priority": priority,
            "timestamp": timestamp
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to submit hook+demo job: {str(e)}")

@app.post("/process-avatar-video")
async def process_avatar_video(request: AvatarVideoRequest):
    """Submit avatar video processing job to queue with FIFO priority"""
    try:
        # Extract timestamp from job ID for FIFO ordering
        timestamp = extract_timestamp_from_job_id(request.jobId)
        
        # Calculate priority: earlier timestamps get higher priority (lower number)
        # Use negative timestamp so earlier submissions have lower numbers (higher priority)
        priority = -timestamp if timestamp > 0 else 5
        
        # Submit task to Celery with timestamp-based priority
        task = celery_app.send_task(
            'process_avatar_video_async',
            args=[request.dict()],
            task_id=request.jobId,
            priority=priority,
            queue='video_processing'  # Use specific queue for video processing
        )
        
        # Get current queue length for position estimate
        queue_length = redis_client.llen('video_processing')
        
        print(f"Submitted avatar job {request.jobId} with timestamp {timestamp} and priority {priority}")
        
        return {
            "job_id": request.jobId,
            "status": "queued",
            "message": "Avatar video processing job submitted to queue",
            "queue_position": queue_length + 1,
            "priority": priority,
            "timestamp": timestamp
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to submit avatar job: {str(e)}")

@app.post("/process-slideshow")
async def process_slideshow(request: SlideshowProcessingRequest):
    """Submit slideshow image processing job to queue with FIFO priority"""
    try:
        print(f"Received slideshow request for job: {request.jobId}")
        print(f"Slideshow data: {json.dumps(request.slideshowData, indent=2)}")
        
        # Extract timestamp from job ID for FIFO ordering
        timestamp = extract_timestamp_from_job_id(request.jobId)
        
        # Calculate priority: earlier timestamps get higher priority (lower number)
        # Use negative timestamp so earlier submissions have lower numbers (higher priority)
        priority = -timestamp if timestamp > 0 else 5
        
        # Submit task to Celery with timestamp-based priority
        task = celery_app.send_task(
            'process_slideshow_async',
            args=[request.dict()],
            task_id=request.jobId,
            priority=priority,
            queue='video_processing'  # Use same queue for consistency
        )
        
        # Get current queue length for position estimate
        queue_length = redis_client.llen('video_processing')
        
        print(f"Submitted slideshow job {request.jobId} with timestamp {timestamp} and priority {priority}")
        
        return {
            "job_id": request.jobId,
            "status": "queued",
            "message": "Slideshow processing job submitted to queue",
            "queue_position": queue_length + 1,
            "priority": priority,
            "timestamp": timestamp
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to submit slideshow job: {str(e)}")

@app.get("/job-status/{job_id}")
async def get_job_status(job_id: str):
    """Get current status of a processing job"""
    try:
        # Get status from Redis
        progress_key = f"job_progress:{job_id}"
        progress_data = redis_client.get(progress_key)
        
        if progress_data:
            status_info = json.loads(progress_data)
            
            # Check if job is still in queue
            if status_info['status'] == 'processing' and status_info['progress'] < 5:
                # Try to get queue position
                queue_length = redis_client.llen('video_processing')
                status_info['queue_position'] = max(1, queue_length)
            
            return status_info
        else:
            # Check if task exists in Celery
            task_result = celery_app.AsyncResult(job_id)
            if task_result.state == 'PENDING':
                return {
                    'status': 'queued',
                    'progress': 0,
                    'message': 'Job is waiting in queue...',
                    'queue_position': redis_client.llen('video_processing')
                }
            elif task_result.state == 'STARTED':
                return {
                    'status': 'processing',
                    'progress': 5,
                    'message': 'Job has started processing...'
                }
            else:
                return {
                    'status': 'not_found',
                    'progress': 0,
                    'message': 'Job not found'
                }
                
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get job status: {str(e)}")

@app.get("/job-stream/{job_id}")
async def stream_job_progress(job_id: str):
    """Stream real-time progress updates for a job"""
    
    async def generate_updates():
        # Subscribe to Redis pub/sub for real-time updates
        pubsub = redis_client.pubsub()
        pubsub.subscribe(f"job_updates:{job_id}")
        
        # Send initial status
        progress_key = f"job_progress:{job_id}"
        initial_data = redis_client.get(progress_key)
        
        if initial_data:
            yield f"data: {initial_data.decode()}\n\n"
        else:
            initial_status = {
                'status': 'queued',
                'progress': 0,
                'message': 'Job is in queue...',
                'queue_position': redis_client.llen('video_processing')
            }
            yield f"data: {json.dumps(initial_status)}\n\n"
        
        # Stream updates
        try:
            while True:
                message = pubsub.get_message(timeout=1.0)
                if message and message['type'] == 'message':
                    data = message['data'].decode()
                    parsed_data = json.loads(data)
                    
                    yield f"data: {data}\n\n"
                    
                    # Stop streaming if job completed or failed
                    if parsed_data.get('status') in ['completed', 'error']:
                        break
                        
                # Check if job is still active every 30 seconds
                await asyncio.sleep(1)
                
        except Exception as e:
            error_data = {
                'status': 'error',
                'progress': 0,
                'message': f'Streaming error: {str(e)}'
            }
            yield f"data: {json.dumps(error_data)}\n\n"
        finally:
            pubsub.unsubscribe(f"job_updates:{job_id}")
            pubsub.close()
    
    return StreamingResponse(
        generate_updates(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive"
        }
    )

@app.get("/queue-stats")
async def get_queue_stats():
    """Get current queue statistics"""
    try:
        # Get queue lengths
        pending_jobs = redis_client.llen('video_processing')
        active_jobs = len(celery_app.control.inspect().active() or {})
        
        # Get worker info
        worker_stats = celery_app.control.inspect().stats()
        worker_count = len(worker_stats) if worker_stats else 0
        
        return {
            "pending_jobs": pending_jobs,
            "active_jobs": active_jobs,
            "worker_count": worker_count,
            "total_capacity": worker_count,  # Assuming 1 job per worker
            "queue_health": "healthy" if worker_count > 0 else "no_workers"
        }
    except Exception as e:
        return {
            "error": str(e),
            "queue_health": "error"
        }

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    try:
        # Check Redis connection
        redis_client.ping()
        
        # Check Celery workers
        worker_stats = celery_app.control.inspect().stats()
        worker_count = len(worker_stats) if worker_stats else 0
        
        return {
            "status": "healthy",
            "message": "Video processing queue server is running",
            "redis_connected": True,
            "active_workers": worker_count
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "message": str(e),
            "redis_connected": False,
            "active_workers": 0
        }

@app.post("/admin/recover-orphaned-jobs")
async def recover_orphaned_jobs():
    """
    Manually trigger recovery of orphaned processing jobs (admin endpoint)
    
    The API server is the sole coordinator for orphaned job recovery to prevent
    race conditions where multiple workers might try to recover the same job.
    """
    try:
        if not supabase_client:
            raise HTTPException(status_code=503, detail="Recovery system not available - no Supabase client")
        
        # Run the recovery check (jobs older than 10 minutes)
        check_and_recover_orphaned_jobs()
        
        return {
            "status": "success",
            "message": "Orphaned job recovery triggered successfully (10min timeout)",
            "currently_processing": list(currently_processing_jobs),
            "recovery_coordinator": "api_server_only"
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to recover orphaned jobs: {str(e)}")

@app.get("/admin/processing-jobs")
async def get_currently_processing_jobs():
    """Get list of currently processing jobs (admin endpoint)"""
    try:
        return {
            "currently_processing": list(currently_processing_jobs),
            "count": len(currently_processing_jobs)
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to get processing jobs: {str(e)}")

@app.get("/music/list")
async def list_music_files():
    """List available music files for hook+demo processing"""
    try:
        music_dir = os.path.join(os.path.dirname(__file__), 'music')
        
        if not os.path.exists(music_dir):
            return {
                "music_files": [],
                "message": "Music directory not found"
            }
        
        # Get all .mp3 files and return their names without extension
        music_files = []
        for filename in os.listdir(music_dir):
            if filename.endswith('.mp3') and not filename.startswith('.'):
                # Remove .mp3 extension for the music name
                music_name = filename[:-4]
                # Convert underscores to spaces and title case for display
                display_name = music_name.replace('_', ' ').title()
                music_files.append({
                    "name": music_name,
                    "display_name": display_name,
                    "filename": filename
                })
        
        # Sort by display name
        music_files.sort(key=lambda x: x['display_name'])
        
        return {
            "music_files": music_files,
            "total_count": len(music_files)
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list music files: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    # Run with multiple workers for the API server
    uvicorn.run(
        "task_queue_server:app", 
        host="0.0.0.0", 
        port=8000,
        workers=4,  # Multiple API workers
        reload=False
    )