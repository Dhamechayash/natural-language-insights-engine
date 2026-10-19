from app.llm import Proposal


class FakeSession:
    def __init__(self, script):
        self.script, self.errors, self.calls = list(script), [], 0

    def propose(self) -> Proposal:
        self.calls += 1
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return Proposal(**item, input_tokens=10, output_tokens=5)

    def report_error(self, message: str) -> None:
        self.errors.append(message)


class FakeLLM:
    """Scripted proposals; narration echoes the first row unless overridden."""

    def __init__(self, script, narration=None):
        self.script, self.narration, self.session, self.history, self.sessions = script, narration, None, None, 0

    def new_sql_session(self, question, profile_text, history):
        self.history, self.sessions = history, self.sessions + 1
        self.session = FakeSession(self.script)
        return self.session

    def narrate(self, question, columns, rows):
        text = self.narration if self.narration is not None else f"The answer is {rows[0][-1]}."
        return text, 7, 3

    def annotate(self, profile_text):
        return {}


def answerable(sql, assumptions=()):
    return {"answerable": True, "reason": "ok", "sql": sql, "assumptions": list(assumptions)}


def refusal(reason="missing data"):
    return {"answerable": False, "reason": reason, "sql": "", "assumptions": []}
