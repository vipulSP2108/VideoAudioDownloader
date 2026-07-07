import streamlit as st
import uuid
from typing import List, Dict

def init_queue():
    """Initialize the queue in Streamlit's session state if it doesn't exist."""
    if 'processing_queue' not in st.session_state:
        st.session_state.processing_queue = []
    if 'history' not in st.session_state:
        st.session_state.history = []

def add_to_queue(url: str, mode: str, quality: str) -> str:
    """Add a new item to the processing queue."""
    item_id = str(uuid.uuid4())
    item = {
        'id': item_id,
        'url': url,
        'mode': mode,
        'quality': quality,
        'status': 'Pending',
        'error': None
    }
    st.session_state.processing_queue.append(item)
    return item_id

def get_queue() -> List[Dict]:
    """Retrieve all current queue items."""
    return st.session_state.processing_queue

def get_history() -> List[Dict]:
    """Retrieve completed/failed items from history."""
    return st.session_state.history

def update_status(item_id: str, status: str, error: str = None):
    """Update the status of a specific queue item."""
    for item in st.session_state.processing_queue:
        if item['id'] == item_id:
            item['status'] = status
            if error:
                item['error'] = error
            
            if status in ['Completed', 'Failed']:
                # Save to history
                history_item = item.copy()
                # Check if it's already in history
                if not any(h['id'] == history_item['id'] for h in st.session_state.history):
                    st.session_state.history.append(history_item)

def remove_from_queue(item_id: str):
    """Remove a specific item from the queue."""
    st.session_state.processing_queue = [
        item for item in st.session_state.processing_queue 
        if item['id'] != item_id
    ]

def clear_completed_from_queue():
    """Remove all Completed or Failed items from the queue (they remain in history)."""
    st.session_state.processing_queue = [
        item for item in st.session_state.processing_queue 
        if item['status'] not in ['Completed', 'Failed']
    ]

def update_item_config(item_id: str, mode: str, quality: str):
    """Update the mode and quality configuration of a queue item."""
    for item in st.session_state.processing_queue:
        if item['id'] == item_id:
            item['mode'] = mode
            item['quality'] = quality
            break
