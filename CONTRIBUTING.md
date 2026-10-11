# Contributing to Claw Sentinel

Thank you for your interest in contributing to **Claw Sentinel**!

## Development Workflow

1. Fork the repository on GitHub.
2. Clone your fork locally:
   ```bash
   git clone https://github.com/Dali5599/claw-sentinel-cli.git
   cd claw-sentinel-cli
   ```
3. Install dependencies in editable mode:
   ```bash
   pip install -e ".[dev]"
   ```
4. Run tests before submitting changes:
   ```bash
   pytest -v
   ```
5. Submit a pull request with a descriptive title and detailed explanation.

## Code Standards
- Adhere to PEP 8 styling.
- Keep standard library usage prioritized for zero external network overhead.
- Ensure all tests pass.
