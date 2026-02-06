"""WebSocket support for real-time progress updates.

This module provides a ConnectionManager class for managing WebSocket connections
and broadcasting real-time updates to connected clients.
"""
from typing import Dict, Set
from fastapi import WebSocket


class ConnectionManager:
    """Manages WebSocket connections for real-time updates."""
    
    def __init__(self):
        # Map of job_id -> set of WebSocket connections
        self.active_connections: Dict[str, Set[WebSocket]] = {}
        # Map of all connected websockets for broadcast
        self.all_connections: Set[WebSocket] = set()
    
    async def connect(self, websocket: WebSocket, job_id: str = None):
        """Accept a WebSocket connection."""
        await websocket.accept()
        self.all_connections.add(websocket)
        
        if job_id:
            if job_id not in self.active_connections:
                self.active_connections[job_id] = set()
            self.active_connections[job_id].add(websocket)
    
    def disconnect(self, websocket: WebSocket, job_id: str = None):
        """Remove a WebSocket connection."""
        self.all_connections.discard(websocket)
        
        if job_id and job_id in self.active_connections:
            self.active_connections[job_id].discard(websocket)
            if not self.active_connections[job_id]:
                del self.active_connections[job_id]
    
    async def send_personal_message(self, message: dict, websocket: WebSocket):
        """Send a message to a specific connection."""
        try:
            await websocket.send_json(message)
        except Exception:
            pass
    
    async def broadcast_to_job(self, job_id: str, message: dict):
        """Broadcast a message to all connections watching a specific job."""
        if job_id in self.active_connections:
            disconnected = set()
            for connection in self.active_connections[job_id]:
                try:
                    await connection.send_json(message)
                except Exception:
                    disconnected.add(connection)
            
            # Clean up disconnected clients
            for conn in disconnected:
                self.disconnect(conn, job_id)
    
    async def broadcast_all(self, message: dict):
        """Broadcast a message to all connected clients."""
        disconnected = set()
        for connection in self.all_connections:
            try:
                await connection.send_json(message)
            except Exception:
                disconnected.add(connection)
        
        # Clean up disconnected clients
        for conn in disconnected:
            self.all_connections.discard(conn)


# Global connection manager instance
manager = ConnectionManager()
