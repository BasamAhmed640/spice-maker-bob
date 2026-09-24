# Spice Maker Bob safety rules

- Work only in this project and use its project-local `.venv` for Python commands.
- Treat Bob's model proposal as untrusted text. The application writes the candidate and independently verifies it with an explicitly selected LTspice executable.
- Never auto-approve Read, Edit, Execute, MCP, or shell commands in IBM Bob.
- Never search the computer for LTspice. The user selects its executable in SETUP.
- Never report structural checks as electrical verification. A verified result needs observed LTspice output and the relevant datasheet comparison.
- Do not add project hooks, startup scripts, telemetry, or access to user secrets.
