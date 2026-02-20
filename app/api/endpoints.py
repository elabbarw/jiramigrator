from fastapi import APIRouter, HTTPException, Depends, File, UploadFile
from sqlalchemy.orm import Session
from typing import Optional, List, Dict, Any
import os
import tempfile
import csv
from pydantic import BaseModel, EmailStr
from datetime import datetime

from app.celery.worker import process_migration, send_email_report
from app.core.config import settings
from app.core.database import get_db
from app.models.job import Job

router = APIRouter()

class MigrationRequest(BaseModel):
    """Model for Jira migration request."""
    vault_url: str
    vault_name: str
    vault_token: str
    export_method: str
    jql: Optional[str] = None
    project_key: Optional[str] = None
    csv_data: Optional[List[str]] = None
    environment: Optional[str] = None
    sharepoint_site: Optional[str] = None
    sharepoint_folder: Optional[str] = None
    api_type: Optional[str] = None
    custom_api_url: Optional[str] = None
    send_email: Optional[bool] = False
    email: Optional[EmailStr] = None
    parallelism: Optional[int] = 5
    
    # Custom fields options
    analyze_fields: Optional[bool] = True
    show_all_fields: Optional[bool] = False
    use_view_screen: Optional[bool] = True
    identifier_field_terms: Optional[List[str]] = None
    important_field_terms: Optional[List[str]] = None
    include_manual_fields: Optional[bool] = False
    manual_fields: Optional[List[str]] = None

class ConfluenceMigrationRequest(BaseModel):
    """Model for Confluence migration request."""
    confluence_url: str
    confluence_username: str
    confluence_token: str
    space_key: Optional[str] = None
    cql: Optional[str] = None
    sharepoint_site: str
    sharepoint_folder: str
    parallelism: Optional[int] = 5
    send_email: Optional[bool] = False
    email: Optional[EmailStr] = None

@router.post("/migration/submit")
async def submit_migration(request: MigrationRequest, db: Session = Depends(get_db)):
    """Submit a migration job."""
    try:
        # Validate required fields
        if not request.jql and not request.project_key and not request.csv_data:
            raise HTTPException(
                status_code=400, 
                detail="One of jql, project_key, or csv_data must be provided"
            )
        if request.project_key is not None and not str(request.project_key).strip():
            raise HTTPException(
                status_code=400,
                detail="project_key cannot be empty when using Project Key selection"
            )
            
        # Validate export method specific fields
        if request.export_method == 'sharepoint':
            if not request.sharepoint_site or not request.sharepoint_folder:
                raise HTTPException(
                    status_code=400,
                    detail="sharepoint_site and sharepoint_folder are required for SharePoint export"
                )
        elif request.export_method == 'api':
            if not request.custom_api_url:
                raise HTTPException(
                    status_code=400,
                    detail="custom_api_url is required for API export"
                )
                    
        # Convert the Pydantic model to a dict for Celery task
        task_data = request.dict()
        
        # Generate JQL from project key if jql not explicitly provided
        project_key = (request.project_key or "").strip()
        if project_key and not request.jql:
            task_data["jql"] = f"project = {project_key}"
            task_data["project_key"] = project_key
        
        # Create job record in database
        job = Job(
            vault_url=request.vault_url,
            vault_name=request.vault_name,
            export_method=request.export_method,
            jql=task_data.get("jql"),
            project_key=request.project_key,
            sharepoint_site=request.sharepoint_site,
            sharepoint_folder=request.sharepoint_folder,
            api_type=request.api_type,
            environment=request.environment,
            custom_api_url=request.custom_api_url,
            status="pending",
            parallelism=request.parallelism or 5,
            send_email=request.send_email or False,
            email=request.email,
            config_data=task_data,
            created_at=datetime.utcnow()
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        
        # Add job_db_id to task data
        task_data["job_db_id"] = job.id
            
        # Queue the task in Celery
        if request.send_email and request.email:
            result = process_migration.apply_async(
                args=[task_data],
                link=send_email_report.s(request.email)
            )
        else:
            result = process_migration.apply_async(args=[task_data])
        
        # Update job with Celery task ID
        job.celery_task_id = result.id
        db.commit()
            
        return {
            "job_id": result.id, 
            "db_job_id": job.id,
            "status": "submitted"
        }
        
    except Exception as e:
        # Log the exception (you might want to add proper logging)
        print(f"Error submitting migration job: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/migration/status/{job_id}")
async def get_migration_status(job_id: str):
    """Get the status of a migration job."""
    try:
        # Import task here to avoid circular imports
        from app.celery.worker import process_migration
        
        # Get task result from Celery
        result = process_migration.AsyncResult(job_id)
        
        response = {
            "job_id": job_id,
            "status": result.status,
        }
        
        # Add result if available
        if result.ready():
            if result.successful():
                response["result"] = result.result
            else:
                response["error"] = str(result.result)
                
        return response
        
    except Exception as e:
        # Log the exception
        print(f"Error getting migration status: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/migration/csv-upload")
async def upload_csv(file: UploadFile = File(...)):
    """Upload a CSV file with Jira issue keys."""
    try:
        # Check if the file is a CSV
        if not file.filename.endswith('.csv'):
            raise HTTPException(status_code=400, detail="File must be a CSV")
            
        # Create a temporary file to store the uploaded CSV
        with tempfile.NamedTemporaryFile(delete=False, suffix='.csv') as temp_file:
            temp_path = temp_file.name
            content = await file.read()
            temp_file.write(content)
            
        # Read the CSV to extract issue keys
        issue_keys = []
        try:
            with open(temp_path, 'r', encoding='utf-8', errors='replace') as f:
                raw_content = f.read().strip()
            
            if not raw_content:
                os.unlink(temp_path)
                raise HTTPException(status_code=400, detail="CSV file is empty")
            
            # Assume comma-delimited CSV
            with open(temp_path, 'r', encoding='utf-8', errors='replace') as csv_file:
                reader = csv.DictReader(csv_file, delimiter=',')
                key_column = None
                if reader.fieldnames:
                    header_map = {h.strip().lower(): h for h in reader.fieldnames}
                    for candidate in ['key', 'keys', 'issue key', 'issue_key', 'issuekey', 'jira key', 'jira_key']:
                        if candidate in header_map:
                            key_column = header_map[candidate]
                            break
                    if not key_column and reader.fieldnames:
                        key_column = reader.fieldnames[0]
                if key_column:
                    issue_part_candidates = ['issue', 'number', 'issue number', 'issue_id', 'id', 'no', 'num', 'issue key']
                    for row in reader:
                        val = row.get(key_column, '').strip()
                        if not val:
                            continue
                        if '-' not in val and reader.fieldnames:
                            orig = {h.strip().lower(): h for h in reader.fieldnames}
                            for cand in issue_part_candidates:
                                if cand in orig:
                                    part = row.get(orig[cand], '').strip()
                                    if part:
                                        combined = val + part if part.startswith('-') else (val + '-' + part if part.isdigit() else val + part)
                                        if combined and '-' in combined:
                                            val = combined
                                            break
                            if '-' not in val and len(reader.fieldnames) >= 2 and key_column != reader.fieldnames[1]:
                                part = row.get(reader.fieldnames[1], '').strip()
                                if part:
                                    combined = val + part if part.startswith('-') else (val + '-' + part if part.isdigit() else val + part)
                                    if combined and '-' in combined:
                                        val = combined
                        issue_keys.append(val)
            
            os.unlink(temp_path)
            return {"filename": file.filename, "issue_count": len(issue_keys), "issues": issue_keys}
            
        except HTTPException:
            raise
        except Exception as csv_error:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise HTTPException(status_code=400, detail=f"Error parsing CSV: {str(csv_error)}")
            
    except Exception as e:
        # Log the exception
        print(f"Error uploading CSV: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/confluence/submit")
async def submit_confluence_migration(request: ConfluenceMigrationRequest, db: Session = Depends(get_db)):
    """Submit a Confluence migration job."""
    try:
        if not request.space_key and not request.cql:
            raise HTTPException(
                status_code=400,
                detail="One of space_key or cql must be provided"
            )

        task_data = request.dict()
        task_data["job_type"] = "confluence"

        # Create job record (reuse Job model with export_method='confluence')
        job = Job(
            vault_url=request.confluence_url,
            vault_name=request.confluence_username,
            export_method="confluence",
            jql=request.cql,
            project_key=request.space_key,
            sharepoint_site=request.sharepoint_site,
            sharepoint_folder=request.sharepoint_folder,
            status="pending",
            parallelism=request.parallelism or 5,
            send_email=request.send_email or False,
            email=request.email,
            config_data=task_data,
            created_at=datetime.utcnow()
        )
        db.add(job)
        db.commit()
        db.refresh(job)

        task_data["job_db_id"] = job.id

        # Import here to avoid circular imports
        from app.celery.worker import process_confluence_migration, send_email_report

        if request.send_email and request.email:
            result = process_confluence_migration.apply_async(
                args=[task_data],
                link=send_email_report.s(request.email)
            )
        else:
            result = process_confluence_migration.apply_async(args=[task_data])

        job.celery_task_id = result.id
        db.commit()

        return {
            "job_id": result.id,
            "db_job_id": job.id,
            "status": "submitted"
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"Error submitting Confluence migration job: {e}")
        raise HTTPException(status_code=500, detail=str(e))
