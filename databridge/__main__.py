"""python -m databridge  ->  runs the server on http://localhost:8000"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("databridge.main:app", host="0.0.0.0", port=8000)
