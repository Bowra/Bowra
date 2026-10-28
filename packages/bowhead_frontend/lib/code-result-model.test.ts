import { describe, expect, it } from "vitest";

import { projectCodeRuns } from "./code-result-model";

describe("projectCodeRuns", () => {
  it("projects running and completed runs without inventing artifact outputs", () => {
    const runs = projectCodeRuns({
      executionSteps: [{
        turnId: "turn_1",
        stepId: "step_1",
        callId: null,
        label: "Computing",
        actor: "lead",
        state: "active",
        codeInput: null,
        stdout: null,
        durationMs: null,
        outputs: [],
      }],
      sections: [{
        id: "sec_1",
        kind: "code_block",
        call_id: "call_secret",
        lang: "python",
        code: "print('safe')",
        stdout_preview: "safe\n",
        output_artifacts: [{
          artifact_id: "art_output",
          filename: "result.csv",
          mime: "text/csv",
        }],
        summary_payload: {
          n_lines: 1,
          statement_summary: "Python script",
          dur_ms: 120,
        },
      }],
    });

    expect(runs).toHaveLength(2);
    expect(runs[0]).toMatchObject({ status: "running", code: null, outputs: [] });
    expect(runs[1]).toMatchObject({
      status: "success",
      code: "print('safe')",
      stdout: "safe\n",
      durationMs: 120,
      outputs: [{ artifactId: "art_output", filename: "result.csv" }],
    });
    expect(JSON.stringify(runs)).not.toContain("call_secret");
  });

  it("does not invent code runs without active execution or durable code sections", () => {
    const runs = projectCodeRuns({
      sections: [],
      executionSteps: [],
    });
    expect(runs).toEqual([]);
  });

  it("keeps a completed persisted Python step and its stable outputs", () => {
    const runs = projectCodeRuns({
      sections: [],
      executionSteps: [{
        turnId: "turn_1",
        stepId: "step_1",
        callId: "call_1",
        label: "运行 Python",
        actor: "subagent",
        state: "done",
        codeInput: {
          language: "python",
          code: "print('durable')",
          truncated: false,
        },
        stdout: "durable\n",
        durationMs: 42,
        outputs: [{
          artifactId: "art_1",
          filename: "result.csv",
          mime: "text/csv",
        }],
      }],
    });

    expect(runs).toEqual([expect.objectContaining({
      key: "event:turn_1:step_1",
      status: "success",
      code: "print('durable')",
      stdout: "durable\n",
      durationMs: 42,
      outputs: [{ artifactId: "art_1", filename: "result.csv", mime: "text/csv" }],
    })]);
  });

  it("deduplicates a persisted Python step when a matching code section exists", () => {
    const runs = projectCodeRuns({
      sections: [{
        id: "sec_1",
        kind: "code_block",
        call_id: "call_1",
        lang: "python",
        code: "print('section wins')",
      }],
      executionSteps: [{
        turnId: "turn_1",
        stepId: "step_1",
        callId: "call_1",
        label: "运行 Python",
        actor: "subagent",
        state: "done",
        codeInput: {
          language: "python",
          code: "print('event')",
          truncated: false,
        },
        stdout: null,
        durationMs: null,
        outputs: [],
      }],
    });

    expect(runs).toHaveLength(1);
    expect(runs[0]).toMatchObject({
      key: "section:sec_1",
      code: "print('section wins')",
    });
  });

  it("keeps transient cells only for the explicit legacy rollback surface", () => {
    const runs = projectCodeRuns({
      sections: [],
      legacyPyCells: [{
        n: 2,
        name: "legacy analysis",
        status: "error",
        code: "raise ValueError()",
        output: "ValueError",
        durMs: 75,
        turnId: "turn_internal",
        subagentId: "agent_internal",
      }],
    });

    expect(runs).toHaveLength(1);
    expect(runs[0]).toMatchObject({
      status: "error",
      code: "raise ValueError()",
      stdout: "ValueError",
    });
  });
});
