import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { TableFrame } from "./table-frame";

afterEach(() => cleanup());

describe("TableFrame", () => {
  it("carries the settings-table-frame base class and ui-table-frame, plus the caller's class and inline style", () => {
    const { container } = render(
      <TableFrame className="legacy-members-frame" style={{ marginTop: 4 }}>
        <table />
      </TableFrame>,
    );
    const frame = container.firstElementChild as HTMLElement;
    expect(frame.className.split(" ")).toEqual(["settings-table-frame", "ui-table-frame", "legacy-members-frame"]);
    expect(frame.style.marginTop).toBe("4px");
  });
});
