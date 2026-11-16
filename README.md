# 🐦 Pigeon General Network (PGN)

**Your Linux. Your AI. Your Pigeon.**

Pigeon General Network (PGN) is a personal AI environment for Linux designed to make interacting with your system more intelligent, flexible, and accessible.

Pigeon is a semi-autonomous AI assistant that can understand user requests, plan tasks, interact with system tools, and analyze the results — while keeping the user in control.

## ✨ Overview

The goal of Pigeon is to create a unified environment where AI can interact with the operating system rather than simply answer questions.

Instead of executing every action manually, you give Pigeon a goal. It determines the steps, selects the appropriate tools, executes permitted actions, and evaluates the results.

Pigeon is designed around one important principle:

**The AI should assist the user, not replace their control over the system.**

## ⚙️ Core Concepts

- **AI-powered planning** — breaks user requests into actionable steps.
- **Tool integration** — interacts with the operating system through dedicated tools.
- **Semi-autonomous execution** — can execute multi-step tasks and evaluate their results.
- **User control** — keeps the user involved in the process.
- **Local LLM support** — designed to work with locally hosted language models.
- **Telegram integration** — provides a remote interface for interacting with Pigeon.

## 🧩 Architecture

Pigeon is built around several core components:

| Component   | Responsibility                                   |
| ----------- | ------------------------------------------------ |
| LLM         | Understands requests and generates responses     |
| Planner     | Determines the steps required to complete a task |
| Tool Loop   | Executes tools and processes their results       |
| Permissions | Defines boundaries for AI actions                |
| Memory      | Provides context across interactions             |

### System Tools

Pigeon is designed to interact with the Linux environment through dedicated tools, including:

- Filesystem operations
- Shell commands
- System operations
- Hyprland integration

Additional integrations may be introduced as the project develops.

## 🛠️ Technology Stack

- **Python** — core application logic
- **LLM** — language model backend
- **llama.cpp / llama-server** — local model inference
- **OpenAI-compatible API** — communication with the model backend
- **aiogram** — Telegram bot integration
- **httpx** — asynchronous HTTP communication

## 🚧 Project Status

Pigeon is currently in early development.

The project already includes a working core, filesystem tools, system integrations, and a Telegram interface.

The architecture and functionality are still evolving. Interfaces, tools, and internal components may change as development progresses.

## 🗺️ Roadmap

Planned areas of development:

- [ ] Improve task planning and execution
- [ ] Expand the system toolset
- [ ] Improve permission management
- [ ] Develop persistent memory
- [ ] Expand desktop environment integration
- [ ] Explore Home Assistant integration
- [ ] Improve documentation and installation process

## 🔒 Philosophy

Pigeon is not intended to be an unrestricted autonomous agent.

It is designed to operate within boundaries defined by the user, with the ability to interact with the system while preserving human control.

**Your system. Your rules. Your Pigeon.**

---

*Pigeon General Network — a personal AI environment for Linux.*
