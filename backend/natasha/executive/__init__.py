"""The executive: the loop that turns a message into verified action and an honest answer.

Order of operations for every turn:
  1. perceive   - attach images/audio/documents, tagged as content with a trust level;
  2. recall     - retrieve the memories, world facts and working context that matter;
  3. decide     - one model call that either answers or proposes tool calls;
  4. act        - every proposed call goes through the tool choke point (policy, approval, audit);
  5. verify     - claims are checked before they are made, not after;
  6. repair     - failures are repaired within a bounded budget, then reported;
  7. answer     - the reply states what actually happened, including what did not.
"""

from .context import ContextBuilder, PreparedContext
from .orchestrator import Executive, Turn, TurnResult, get_executive

__all__ = ["ContextBuilder", "PreparedContext", "Executive", "Turn", "TurnResult", "get_executive"]
