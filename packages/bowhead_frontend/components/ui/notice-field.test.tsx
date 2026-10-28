import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { Field } from "./field";
import { Notice } from "./notice";

afterEach(cleanup);

describe("Notice / Field 的 className", () => {
  it("Notice 把 className 接在基础 class 之后；不传时只有基础 class", () => {
    const { rerender } = render(<Notice tone="error" className="legacy-notice--mt12">出错了</Notice>);
    const el = screen.getByRole("alert");
    expect(el.className).toBe("settings-notice settings-notice--error legacy-notice--mt12");
    rerender(<Notice tone="muted">提示</Notice>);
    expect(screen.getByRole("status").className).toBe("settings-notice settings-notice--muted");
  });

  it("Field 把 className 接在 settings-field 之后；不传时只有 settings-field", () => {
    const { container, rerender } = render(
      <Field id="f1" label="空间" className="legacy-space-members-workspace-field"><input id="f1" /></Field>,
    );
    expect((container.firstChild as HTMLElement).className).toBe("settings-field legacy-space-members-workspace-field");
    rerender(<Field id="f1" label="空间"><input id="f1" /></Field>);
    expect((container.firstChild as HTMLElement).className).toBe("settings-field");
  });
});
