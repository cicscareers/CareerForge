# Contributing

Hey! Thanks for wanting to help out with this project. Here's how to get started.

This is a pretty small Python tool — it moves employer data from a Notion CSV export into CareerForge (a Bubble.io app) using browser automation. Not too complicated, but there's plenty of room to make it better.

## Setting Up for Development

1. Fork and clone the repo
2. Create a virtual environment:
   ```bash
   python -m venv venv && source venv/bin/activate
   ```
3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
4. Install Playwright browsers:
   ```bash
   playwright install chromium
   ```
5. You'll need access to a running CareerForge instance to test the browser automation stuff. Without it you can still work on the CSV parsing and checkpoint logic, but you won't be able to run the full pipeline.

## Project Structure

There are only 6 Python files, so it's pretty easy to get around. Check out [ARCHITECTURE.md](ARCHITECTURE.md) for a deep dive into how everything fits together.

## How to Make Changes

- Create a new branch: `git checkout -b your-feature-name`
- Make your changes
- Test manually — there's no test suite yet (that'd be a great contribution!)
- Commit with clear messages
- Open a pull request

## Commit Message Guidelines

Keep it simple:

- Use present tense ("add feature" not "added feature")
- Keep the first line under 72 characters
- Be descriptive: `fix employer search for names with special characters` > `fix bug`

## Areas Where Help Is Needed

If you're looking for something to work on, here are some ideas:

- **Test suite** — unit tests for `csv_parser.py` and `checkpoint.py` would be huge
- **Better error recovery** — the Bubble.io DOM changes sometimes and things break
- **More CareerForge fields** — we only transfer some fields right now
- **Config file** — move hardcoded constants to a `.env` or YAML file
- **Employer name matching** — names don't always match between Notion and CareerForge
- **Docs** — there's always room for better documentation

## Code Style

- Keep it simple and readable
- Add docstrings to new functions
- Follow the patterns already in the codebase
- No need for strict PEP8 — just be consistent with what's there

## Questions?

If you're unsure about anything or have questions, just open an issue. No question is too small!
