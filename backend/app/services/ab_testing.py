"""ABTestingService: deterministic prompt A/B experiment assignment + exposure tracking (FR-007 part 3).

A/B experiments allow safe rollout of new prompt versions. Traffic is split
deterministically (sticky per subject) between a control and challenger version,
exposures are recorded, and an experiment can be ended by declaring a winner.

Design decisions for v1:
  - Experiments are GLOBAL (one active experiment at a time, not per-org).
    This keeps the implementation simple and matches the recommended global-active
    experiment scope described in the task.
  - Subject key is derived from (org_id, user_id, session_id) by the caller and
    passed in.  The service does NOT construct the key itself — different callers
    may have different ideas of what constitutes a "subject" (e.g. an anonymous
    session vs an authenticated user).
  - Assignment is deterministic: hash(experiment.name + subject_key) % 100 < split_pct
    → challenger, else control.  The same subject always gets the same variant.
  - Exposure recording uses INSERT OR IGNORE so re-exposure of the same subject
    to the same experiment is idempotent (no double-counting).
  - Outcome measurement is intentionally v1-simple: exposure counts per variant.
    Joining to feedback/quality signals is a future extension.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class ABExperiment:
    """An A/B experiment row."""
    id: int
    name: str
    control_version: str
    challenger_version: str
    split_pct: int
    status: str  # 'active' | 'ended'
    winner: Optional[str]  # 'control' | 'challenger' | None
    created_at: str
    ended_at: Optional[str]


@dataclass
class ExposureCount:
    """Exposure counts for one variant."""
    variant: str  # 'control' | 'challenger'
    count: int


@dataclass
class ExperimentWithCounts:
    """An experiment with per-variant exposure counts."""
    experiment: ABExperiment
    control_exposures: int
    challenger_exposures: int


class ABTestingService:
    """Synchronous CRUD + assignment service for prompt A/B experiments."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db
        self._db.row_factory = sqlite3.Row

    # ------------------------------------------------------------------
    # Assignment
    # ------------------------------------------------------------------

    @staticmethod
    def assign(experiment: ABExperiment, subject_key: str) -> str:
        """Determine which variant a subject falls into (deterministic, sticky).

        Assignment is deterministic: hash(experiment.name + subject_key) % 100
        determines the bucket.  Subjects below split_pct get 'challenger',
        the rest get 'control'.  The same subject always gets the same variant.

        Args:
            experiment: The A/B experiment to assign against.
            subject_key: A stable identifier for the subject
                (e.g. hash of org_id/user_id/session).

        Returns:
            'challenger' or 'control'.
        """
        bucket = int(
            hashlib.md5(
                (experiment.name + subject_key).encode("utf-8")
            ).hexdigest(),
            16,
        ) % 100
        return "challenger" if bucket < experiment.split_pct else "control"

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_active_experiment(self) -> Optional[ABExperiment]:
        """Return the currently-active global experiment, or None.

        Returns the oldest experiment when multiple are somehow active (should not
        happen in normal operation — callers should end one experiment before
        starting another).
        """
        row = self._db.execute(
            """
            SELECT id, name, control_version, challenger_version,
                   split_pct, status, winner, created_at, ended_at
            FROM   prompt_ab_experiments
            WHERE  status = 'active'
            ORDER  BY created_at ASC
            LIMIT  1
            """
        ).fetchone()
        if row is None:
            return None
        return self._row_to_experiment(row)

    def get_experiment(self, experiment_id: int) -> Optional[ABExperiment]:
        """Return a specific experiment by ID, or None."""
        row = self._db.execute(
            """
            SELECT id, name, control_version, challenger_version,
                   split_pct, status, winner, created_at, ended_at
            FROM   prompt_ab_experiments
            WHERE  id = ?
            """,
            (experiment_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_experiment(row)

    def list_experiments(self) -> List[ExperimentWithCounts]:
        """Return all experiments with per-variant exposure counts."""
        experiments = self._db.execute(
            """
            SELECT id, name, control_version, challenger_version,
                   split_pct, status, winner, created_at, ended_at
            FROM   prompt_ab_experiments
            ORDER  BY created_at DESC
            """
        ).fetchall()

        result: List[ExperimentWithCounts] = []
        for row in experiments:
            exp = self._row_to_experiment(row)
            counts = self._db.execute(
                """
                SELECT assigned_variant, COUNT(*) as cnt
                FROM   prompt_ab_exposures
                WHERE  experiment_id = ?
                GROUP  BY assigned_variant
                """,
                (exp.id,),
            ).fetchall()
            control_count = 0
            challenger_count = 0
            for count_row in counts:
                if count_row["assigned_variant"] == "control":
                    control_count = count_row["cnt"]
                else:
                    challenger_count = count_row["cnt"]
            result.append(
                ExperimentWithCounts(
                    experiment=exp,
                    control_exposures=control_count,
                    challenger_exposures=challenger_count,
                )
            )
        return result

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def create_experiment(
        self,
        name: str,
        control_version: str,
        challenger_version: str,
        split_pct: int = 50,
    ) -> ABExperiment:
        """Create and activate a new A/B experiment.

        Args:
            name: Unique experiment name.
            control_version: Version string for the control arm.
            challenger_version: Version string for the challenger arm.
            split_pct: Percentage of traffic to send to challenger (0-100).

        Returns:
            The created experiment.

        Raises:
            sqlite3.IntegrityError: if name already exists.
        """
        cursor = self._db.execute(
            """
            INSERT INTO prompt_ab_experiments
                (name, control_version, challenger_version, split_pct)
            VALUES (?, ?, ?, ?)
            """,
            (name, control_version, challenger_version, split_pct),
        )
        self._db.commit()
        return self.get_experiment(int(cursor.lastrowid))  # type: ignore[arg-type]

    def end_experiment(self, experiment_id: int, winner: str) -> ABExperiment:
        """End an experiment and declare the winner.

        Args:
            experiment_id: ID of the experiment to end.
            winner: 'control' or 'challenger'.

        Raises:
            ValueError: if experiment_id not found or winner is invalid.
        """
        if winner not in ("control", "challenger"):
            raise ValueError(f"winner must be 'control' or 'challenger', got {winner!r}")

        rows_updated = self._db.execute(
            """
            UPDATE prompt_ab_experiments
            SET    status = 'ended',
                   winner = ?,
                   ended_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            WHERE  id = ? AND status = 'active'
            """,
            (winner, experiment_id),
        ).rowcount
        self._db.commit()

        if rows_updated == 0:
            # Check if experiment exists
            exp = self.get_experiment(experiment_id)
            if exp is None:
                raise ValueError(f"No experiment with id={experiment_id}")
            # Experiment exists but is already ended
            raise ValueError(
                f"Experiment id={experiment_id} is already {exp.status}"
            )

        return self.get_experiment(experiment_id)  # type: ignore[arg-type]

    def record_exposure(
        self,
        experiment_id: int,
        subject_key: str,
        variant: str,
    ) -> None:
        """Record a subject's exposure to an experiment variant.

        Uses INSERT OR IGNORE so this is idempotent: re-exposure of the same
        subject to the same experiment does not create a duplicate row.

        Args:
            experiment_id: ID of the experiment.
            subject_key: Stable identifier for the subject.
            variant: 'control' or 'challenger'.
        """
        self._db.execute(
            """
            INSERT OR IGNORE INTO prompt_ab_exposures
                (experiment_id, subject_key, assigned_variant)
            VALUES (?, ?, ?)
            """,
            (experiment_id, subject_key, variant),
        )
        self._db.commit()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_experiment(row: sqlite3.Row) -> ABExperiment:
        d = dict(row)
        return ABExperiment(
            id=d["id"],
            name=d["name"],
            control_version=d["control_version"],
            challenger_version=d["challenger_version"],
            split_pct=d["split_pct"],
            status=d["status"],
            winner=d.get("winner"),
            created_at=d["created_at"],
            ended_at=d.get("ended_at"),
        )
