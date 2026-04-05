FROM node:22-slim

RUN apt-get update && apt-get install -y python3 python3-pip && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --break-system-packages -r requirements.txt

COPY agent.py .

# Mount points:
#   /app/data    — config.json + sessions.json (persistent)
#   /root/.claude — Claude CLI auth (from host ~/.claude)
VOLUME ["/app/data", "/root/.claude"]

CMD ["python3", "agent.py", "-f", "/app/data/config.json"]
