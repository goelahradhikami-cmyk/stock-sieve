"""Database connector for Streamlit UI — cached queries."""
import os
import sys

# Ensure project root in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

import streamlit as st

from src.data.evaluation_db import EvaluationDB
from src.utils.logger import get_logger

logger = get_logger(__name__)


@st.cache_resource
def get_db() -> EvaluationDB:
    """Get cached EvaluationDB connection."""
    db_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "data", "evaluation.db")
    db = EvaluationDB(db_path)
    db.init_db()
    try:
        db.migrate_v2_1()
    except Exception as e:
        logger.warning("db_connector: migrate_v2_1 failed: %s", e)
    return db
