# MCP Web Research Agent for macOS

A Python [Model Context Protocol](https://modelcontextprotocol.io) (MCP) agent/server that gives a local AI assistant tools for:

- `search_web` — public web search via DuckDuckGo HTML results
- `fetch_url` — fetch a public web page and extract readable text
- `save_note` — save research notes as Markdown/text files in a folder you choose

The project also includes `agent.py`, a small local bridge that connects **Ollama** to the MCP server. Ollama runs the LLM; this project provides the MCP tools and the tool-calling agent loop.

> Note: Ollama itself is a model server, not a native MCP client. To use Ollama with MCP tools, run `agent.py` here or another MCP bridge/client.

## What you need

- MacBook Pro with macOS
- Python 3.11 or newer (the `mcp` package requires Python 3.10+; setup prefers 3.13/3.12/3.11)
- [Ollama](https://ollama.com) installed and running
- A tool-calling local model. Recommended starting point:
  - `qwen2.5:7b` for 16 GB RAM Macs
  - `qwen2.5:14b` if you have enough RAM/performance
  - `qwen3:14b` if your Ollama version supports it well

## 1. Install

Open Terminal and run:

```bash
cd ~/Projects
git clone <your-repo-url> mcp-web-research-agent  # or copy this folder here
cd ~/Projects/mcp-web-research-agent

python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

If you do not have Python 3.11+:

```bash
brew install python
```

## 2. Install and start Ollama

Install Ollama from <https://ollama.com> or with Homebrew:

```bash
brew install --cask ollama
```

Open the Ollama app once, then pull a model:

```bash
ollama pull qwen2.5:7b
ollama serve
```

In another Terminal tab, verify Ollama is running:

```bash
curl http://localhost:11434/api/tags
```

## 3. Run the local Ollama MCP agent

From the project folder:

```bash
cd ~/Projects/mcp-web-research-agent
source .venv/bin/activate
python agent.py
```

Then ask something like:

```text
Research recent MCP news, open the two best sources, summarize them, and save the summary as mcp-news.md.
```

One-shot mode:

```bash
python agent.py "Research current MCP SDK best practices and save notes."
```

Use a different model:

```bash
python agent.py --model qwen2.5:14b
```

Choose where notes are saved:

```bash
python agent.py --notes-dir ~/Documents/research-notes
```

## 4. Use with Claude Desktop

If you want Claude Desktop to connect directly to the MCP server, edit:

```text
~/Library/Application Support/Claude/claude_desktop_config.json
```

Add:

```json
{
  "mcpServers": {
    "web-research": {
      "command": "/Users/YOUR_USERNAME/Projects/mcp-web-research-agent/.venv/bin/python",
      "args": [
        "/Users/YOUR_USERNAME/Projects/mcp-web-research-agent/server.py"
      ],
      "env": {
        "MCP_NOTES_DIR": "/Users/YOUR_USERNAME/Documents/MCP-research-notes"
      }
    }
  }
}
```

Replace `YOUR_USERNAME` with your Mac username. Create the file if it does not exist. Restart Claude Desktop after editing.

## 5. Use with Cursor

Create or edit `.cursor/mcp.json` in a workspace:

```json
{
  "mcpServers": {
    "web-research": {
      "command": "/Users/YOUR_USERNAME/Projects/mcp-web-research-agent/.venv/bin/python",
      "args": [
        "/Users/YOUR_USERNAME/Projects/mcp-web-research-agent/server.py"
      ],
      "env": {
        "MCP_NOTES_DIR": "/Users/YOUR_USERNAME/Documents/MCP-research-notes"
      }
    }
  }
}
```

Then restart Cursor or reload its MCP settings.

## Tool reference

### `search_web(query: str, max_results: int = 5)`

Returns search results as JSON:

```json
{
  "query": "Model Context Protocol",
  "results": [
    {
      "title": "Example",
      "url": "https://example.com",
      "snippet": "..."
    }
  ]
}
```

### `fetch_url(url: str, max_chars: int = 8000)`

Fetches an `http`/`https` URL and returns extracted text. It avoids JavaScript rendering, so it works best on normal HTML pages.

### `save_note(filename: str, content: str)`

Saves a note to `MCP_NOTES_DIR`. The default directory is:

```text
~/MCPWebResearch/notes
```

The tool sanitizes filenames and blocks path traversal.

## Configuration

Environment variables:

- `OLLAMA_MODEL` — default model used by `agent.py`; default is `qwen2.5:7b`
- `OLLAMA_URL` — OpenAI-compatible Ollama chat endpoint; default is `http://localhost:11434/v1/chat/completions`
- `MCP_NOTES_DIR` — directory for saved notes

Example:

```bash
export OLLAMA_MODEL=qwen2.5:14b
export MCP_NOTES_DIR=~/Documents/research-notes
python agent.py
```

## Troubleshooting

### `Connection refused` to `localhost:11434`

Ollama is not running. Start it with:

```bash
ollama serve
```

### The agent does not call tools

Use a model with strong tool-calling support. `qwen2.5:7b`, `qwen2.5:14b`, and similar Qwen models are good starting points.

### A page returns little text

Some websites block non-browser clients or require JavaScript. Try a different source, or use `search_web` and `fetch_url` together.

### Claude Desktop does not show the server

Double-check that:

- The Python path points to `.venv/bin/python` inside this project
- The `server.py` path is absolute
- The JSON file has valid syntax
- You fully restarted Claude Desktop

## Files

- `server.py` — MCP server with web research tools
- `agent.py` — local Ollama-powered MCP client/agent loop
- `requirements.txt` — Python dependencies

## Safety notes

- This server can fetch public URLs and search the public web.
- It can write files only into `MCP_NOTES_DIR`.
- It does not execute shell commands.
- Review saved notes and citations before relying on them.
