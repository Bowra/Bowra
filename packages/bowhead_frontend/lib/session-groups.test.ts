import { describe, expect, it } from "vitest";

import { adaptSessionGroups } from "./session-groups";

describe("adaptSessionGroups", () => {
  it("normalizes Gateway session DTOs without dropping optional state", () => {
    expect(
      adaptSessionGroups([
        {
          id: "today",
          label: "TODAY",
          items: [
            {
              id: "s_1",
              title: "Revenue review",
              state: "done",
              unread: true,
              pinned: true,
              archived: false,
              skill_applied: "revenue-analysis",
            },
          ],
        },
      ]),
    ).toEqual([
      {
        id: "today",
        label: "TODAY",
        items: [
          {
            id: "s_1",
            title: "Revenue review",
            state: "done",
            intents: [],
            time: "",
            updatedAt: null,
            unread: true,
            pinned: true,
            archived: false,
            skillApplied: "revenue-analysis",
          },
        ],
      },
    ]);
  });

  it("carries the Gateway's ISO updated_at to the row model", () => {
    const [group] = adaptSessionGroups([
      {
        id: "yest",
        label: "YESTERDAY",
        items: [{ id: "s_3", title: "t", time: "1d ago", updated_at: "2026-10-07T01:00:00+00:00", state: "done" }],
      },
    ]);
    expect(group.items[0].updatedAt).toBe("2026-10-07T01:00:00+00:00");
  });

  it("keeps explicit intents/time and tolerates a null skill", () => {
    const [group] = adaptSessionGroups([
      {
        id: "older",
        label: "OLDER",
        items: [
          {
            id: "s_2",
            title: "Inventory",
            state: "active",
            intents: ["lookup"],
            time: "2d",
            skill_applied: null,
          },
        ],
      },
    ]);

    expect(group.items[0]).toMatchObject({
      intents: ["lookup"],
      time: "2d",
      skillApplied: null,
    });
  });
});
