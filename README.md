# ⚖️ Madhav.ai Backend

Backend system for **Madhav.ai** — an AI-powered legal research and analysis platform designed to assist lawyers, students, and researchers with case law, legal insights, and intelligent document processing.

---

## 🚀 Features

### 🔹 Core Modes

**Normal Mode**

* Case lookup and browsing
* Court, year, and keyword-based search
* Case summaries and structured data

**Research Mode**

* Input: Client facts / legal problem
* Output: Relevant cases, legal issues, arguments
* AI-powered legal reasoning

**Study Mode**

* Legal chatbot for students
* Concept explanations (IPC, CrPC, Constitution)
* Case breakdown and learning assistance

---

## 🧠 AI Capabilities

* Multi-model AI orchestration (GPT, Claude, etc.)
* Legal issue spotting from facts
* Case similarity ranking
* Paragraph-level analysis
* Future-ready self-learning model (**X-mini**)

---

## 🏗️ Tech Stack

* **Backend Framework:** FastAPI
* **Database:** PostgreSQL
* **AI Integration:** OpenAI / Multi-model routing
* **Search System:** Custom legal retrieval pipeline
* **Containerization:** Docker

---

## ⚙️ Setup Instructions

### 1️⃣ Clone Repository

```bash
git clone https://github.com/aryan-iconic/Madhav.ai_backend.git
cd Madhav.ai_backend
```

---

### 2️⃣ Create Virtual Environment

```bash
python -m venv .venv
```

**Activate:**

Windows:

```bash
.venv\Scripts\activate
```

Mac/Linux:

```bash
source .venv/bin/activate
```

---

### 3️⃣ Install Dependencies

```bash
pip install -r requirements.txt
```

---

### 4️⃣ Setup Environment Variables

Create a `.env` file in root:

```
DATABASE_URL=postgresql://username:password@localhost:5432/legal_db
OPENAI_API_KEY=your_api_key
```

---

### 5️⃣ Run PostgreSQL (Docker)

```bash
docker run -d ^
  --name postgres-madhav ^
  -e POSTGRES_PASSWORD=postgres ^
  -e POSTGRES_DB=legal_knowledge_graph ^
  -p 5432:5432 ^
  postgres:15
```

---

### 6️⃣ Run Backend

```bash
uvicorn main:app --reload
```

Server runs at:

```
http://localhost:8000
```

---

## 📡 API Modes

| Mode          | Description                |
| ------------- | -------------------------- |
| normal_mode   | Case search & browsing     |
| research_mode | Legal reasoning from facts |
| study_mode    | Learning + chatbot         |

---

## 🧪 Example Queries

```
Samatha v State of AP
Supreme Court 2024 judgments
SC/ST land dispute cases
Client denied property rights by family
```

---

## 🔐 Future Enhancements

* Self-learning AI (X-mini)
* Case importance scoring
* Legal document generator
* IPC / BNS / CrPC cross-referencing
* Judge reasoning simulation

---

## 👥 Contribution

1. Fork the repo
2. Create a branch (`feature/your-feature`)
3. Commit changes
4. Push and create PR

---

## 📌 Notes

* Do not commit `.env` or secrets
* Use virtual environment
* Follow clean code practices

---

## 👨‍💻 Author

**Aryan Gupta**
Founder — Madhav.ai

---

## ⭐ Support

If you like this project, give it a ⭐ on GitHub!
