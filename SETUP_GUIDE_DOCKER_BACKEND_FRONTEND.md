# 🚀 Complete Setup Guide — Docker, PostgreSQL, Backend & Frontend

**Status:** April 10, 2026 | Madhav.AI Legal Knowledge Graph  
**Last Updated:** Setup verified with `pg-legal` container containing 20,083 cases

---

## 📋 Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│                      FRONTEND                            │
│              (React/HTML at port 3000)                   │
└────────────────────────┬────────────────────────────────┘
                         │ HTTP/REST
┌────────────────────────▼────────────────────────────────┐
│                 BACKEND (FastAPI)                        │
│         (Python at port 8000 / port 80)                 │
│  - Chat endpoint                                         │
│  - Search endpoints (normal, semantic, relationship)     │
│  - Document upload                                       │
│  - Citation extractor                                    │
└────────────────────────┬────────────────────────────────┘
                         │ psycopg2
┌────────────────────────▼────────────────────────────────┐
│            POSTGRESQL (Docker Container)                 │
│         Port 5432 (postgres-madhav or pg-legal)          │
│  - 20,083 legal cases                                    │
│  - 260,917 paragraphs                                    │
│  - 154,468 citations                                     │
│  Tables: legal_cases, legal_paragraphs, case_citations   │
└─────────────────────────────────────────────────────────┘
```

---

## 🐳 1. DOCKER & POSTGRESQL SETUP

### Prerequisites
- ✅ Docker installed (`docker --version` to check)
- ✅ PostgreSQL client (comes with Docker)
- ✅ Port 5432 available

### Step 1: Check Running Containers

```powershell
docker ps -a
```

You should see:
- ✅ `pg-legal` (with your data) — **USE THIS ONE**
- ❌ `postgres-madhav` (empty) — can stop this
- ❌ Other old containers (can remove)

### Step 2: Start PostgreSQL with Your Data

**If `pg-legal` is running:**
```powershell
docker ps | findstr pg-legal
# Should show: STATUS = Up
```

**If `pg-legal` is stopped:**
```powershell
docker start pg-legal
docker ps
```

**If port 5432 is already taken:**
```powershell
# Stop the blocking container
docker stop postgres-madhav

# Then start pg-legal
docker start pg-legal
```

### Step 3: Verify Database Connection

```powershell
# Connect to database
docker exec -it pg-legal psql -U postgres -d legal_knowledge_graph

# Inside psql, check tables:
\dt
```

**Expected Output:**
```
       List of relations
 Schema |        Name         | Type  | Owner
--------+---------------------+-------+----------
 public | case_acts           | table | postgres
 public | case_citations      | table | postgres
 public | case_legal_references | table | postgres
 public | case_subjects       | table | postgres
 public | case_topics         | table | postgres
 public | legal_cases         | table | postgres
 public | legal_paragraphs    | table | postgres
(7 rows)
```

**Check Data Count:**
```sql
SELECT COUNT(*) FROM legal_cases;      -- Should show ~20,083
SELECT COUNT(*) FROM legal_paragraphs; -- Should show ~260,917
SELECT COUNT(*) FROM case_citations;   -- Should show ~154,468
\q  -- Exit psql
```

---

## ⚙️ 2. BACKEND SETUP (FastAPI)

### Prerequisites
- ✅ Python 3.9+
- ✅ Virtual environment (`.venv`)
- ✅ Requirements installed

### Step 1: Activate Virtual Environment

```powershell
cd d:\Madhav_ai
.venv\Scripts\Activate.ps1
```

You should see `(.venv)` prefix in terminal.

### Step 2: Verify Environment Variables

Check that `.env` exists in database folder:

```powershell
cat database\.env
```

**Expected Content:**
```
DB_HOST=localhost
DB_PORT=5432
DB_NAME=legal_knowledge_graph
DB_USER=postgres
DB_PASSWORD=postgres
OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=llama3
LLM_TIMEOUT=60
API_HOST=0.0.0.0
API_PORT=8000
```

If missing, create it:
```powershell
cp database\env.example database\.env
# Edit database\.env and set correct values
```

### Step 3: Start Backend Server

```powershell
# From d:\Madhav_ai directory
python -m uvicorn Backend.main:app --reload --port 8000
```

**Expected Output:**
```
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
INFO:Backend.main:✅ Database connection pool ready
INFO:     Application startup complete.
```

### Step 4: Test Backend API

**Check if server is running:**
```powershell
curl http://localhost:8000/docs
```

This opens the **Swagger API documentation** at `http://localhost:8000/docs`

**Test a chat request:**
```powershell
$body = @{
    query = "What is bail?"
    mode = "normal"
} | ConvertTo-Json

curl -Method POST `
  -Headers @{"Content-Type"="application/json"} `
  -Body $body `
  http://localhost:8000/chat
```

---

## 🎨 3. FRONTEND SETUP (React/HTML)

### Option A: Simple HTML Frontend (Static Files)

**Location:** `d:\Madhav_ai\frontend\`

```powershell
# Just open in browser
start file:///d:/Madhav_ai/frontend/index.html
```

Or use a simple server:
```powershell
cd d:\Madhav_ai\frontend
python -m http.server 3000
# Open http://localhost:3000
```

### Option B: React Frontend (if available)

```powershell
cd d:\Madhav_ai\frontend
npm install          # Install dependencies
npm start            # Start dev server at http://localhost:3000
```

### Frontend API Endpoints

The frontend connects to backend at:
```
Backend URL: http://localhost:8000
```

Make sure this is configured in:
- `frontend/js/api.js` (or similar)
- `frontend/config.js` (if exists)

---

## 🔄 4. FULL START-UP WORKFLOW

### Quick Start (All Services)

**Terminal 1 — PostgreSQL (already running):**
```powershell
docker ps
# Verify pg-legal is running
```

**Terminal 2 — Backend:**
```powershell
cd d:\Madhav_ai
.venv\Scripts\Activate.ps1
python -m uvicorn Backend.main:app --reload --port 8000
```

**Terminal 3 — Frontend:**
```powershell
cd d:\Madhav_ai\frontend
python -m http.server 3000
# Or: npm start
```

**Access the Application:**
- 🎨 **Frontend:** http://localhost:3000
- ⚙️ **Backend API:** http://localhost:8000/docs
- 🗄️ **Database:** localhost:5432 (via psycopg2 only, no UI)

---

## 🛠️ 5. QUICK REFERENCE COMMANDS

### Docker Commands

```powershell
# View all containers
docker ps -a

# Start specific container
docker start pg-legal

# Stop specific container
docker stop pg-legal

# View container logs
docker logs pg-legal            # Last 100 lines
docker logs -f pg-legal         # Follow logs (live)

# Connect to database directly
docker exec -it pg-legal psql -U postgres -d legal_knowledge_graph

# Remove old containers (if not using)
docker rm postgres-madhav
docker rm elasticsearch
```

### Backend Commands

```powershell
# Activate virtual environment
.venv\Scripts\Activate.ps1

# Start development server
python -m uvicorn Backend.main:app --reload --port 8000

# Start production server
python -m uvicorn Backend.main:app --host 0.0.0.0 --port 8000

# Stop server: Press Ctrl+C
```

### Database Commands

```powershell
# Check data counts
docker exec pg-legal psql -U postgres -d legal_knowledge_graph -c \
  "SELECT COUNT(*) FROM legal_cases;"

# Export data to SQL
docker exec pg-legal pg_dump -U postgres legal_knowledge_graph > backup.sql

# Query from command line
docker exec pg-legal psql -U postgres -d legal_knowledge_graph \
  -c "SELECT case_name, court FROM legal_cases LIMIT 5;"
```

---

## ❌ 6. TROUBLESHOOTING

### "Connection refused" error on port 5432

**Problem:** PostgreSQL not running  
**Solution:**
```powershell
docker ps  # Check if pg-legal is running
docker start pg-legal
```

### "Port 5432 already in use"

**Problem:** Multiple PostgreSQL containers competing  
**Solution:**
```powershell
docker ps -a  # Find all running containers
docker stop postgres-madhav  # Stop the empty one
docker start pg-legal  # Start the one with data
```

### "relation 'legal_cases' does not exist"

**Problem:** Database schema not created OR wrong container connected  
**Verify:**
```powershell
# Check which container is running
docker ps

# Verify table exists
docker exec pg-legal psql -U postgres -d legal_knowledge_graph -c "\dt"
```

### Backend can't connect to database

**Problem:** Environment variables not set or database address wrong  
**Solution:**
```powershell
# Check .env file exists
cat database\.env

# Restart backend
# Press Ctrl+C to stop
python -m uvicorn Backend.main:app --reload --port 8000
```

### Frontend can't reach backend

**Problem:** Backend URL misconfigured  
**Solution:**
```powershell
# Check backend is running on port 8000
curl http://localhost:8000/docs

# Update frontend config to:
const BACKEND_URL = "http://localhost:8000"
```

### "Database connection failed: no results to fetch"

**Note:** This is a non-critical warning during setup. It happens during initial connection test. **Ignore if tables are created and API works.**

---

## 📊 7. SYSTEM STATUS CHECKS

### Check Everything is Working

```powershell
# 1. Docker
docker ps  # pg-legal should be running

# 2. Database
docker exec pg-legal psql -U postgres -d legal_knowledge_graph -c \
  "SELECT COUNT(*) as total_cases FROM legal_cases;"

# 3. Backend
curl http://localhost:8000/docs

# 4. Check logs
docker logs pg-legal | tail -20
```

### Expected Status

✅ Docker: `pg-legal` container running  
✅ Database: Contains 20,083 cases  
✅ Backend: FastAPI running on port 8000  
✅ Frontend: Accessible at http://localhost:3000  

---

## 📁 8. FILE STRUCTURE

```
d:\Madhav_ai/
├── Backend/                    # FastAPI application
│   ├── main.py                # Entry point
│   ├── db.py                  # Database connection
│   ├── models.py              # Pydantic models
│   ├── retrieval/             # Search logic
│   │   ├── normal_mode.py
│   │   ├── semantic_mode.py
│   │   └── relationship_mode.py
│   └── ...
├── frontend/                   # React/HTML frontend
│   ├── index.html
│   ├── css/
│   └── js/
├── database/                   # Database utilities
│   ├── .env                    # ⚠️ IMPORTANT: Set DB credentials here
│   ├── schema_postgresql*.sql  # Database schema
│   ├── setup_hybrid_system.py  # Database initialization
│   └── ...
├── .venv/                      # Python virtual environment
└── md files/                   # Documentation (this file)
```

---

## 🚀 9. NEXT STEPS

After successful setup:

1. **Test Search:**
   - Open http://localhost:3000
   - Search for "bail" or "contract"
   - Verify results appear

2. **Load More Data (if needed):**
   ```powershell
   python database/data_migration.py
   ```

3. **Generate Embeddings (for semantic search):**
   ```powershell
   python database/embedding_generator.py
   ```

4. **Extract Citations (for relationship search):**
   ```powershell
   python database/citation_extractor.py
   ```

5. **Production Deployment:**
   - Use multi-container Docker Compose
   - Configure proper environment variables
   - Set up SSL/HTTPS
   - Configure CORS for frontend domain
   - Use production ASGI server (Gunicorn)

---

## 📞 CONTACT & SUPPORT

If you encounter issues:

1. **Check logs:** `docker logs pg-legal`
2. **Verify database:** `docker exec pg-legal psql ...`
3. **Test backend:** http://localhost:8000/docs
4. **Review existing issues:** See `ERRORS_AND_FIXES.md`

---

**Happy legal searching! 🏛️⚖️**
