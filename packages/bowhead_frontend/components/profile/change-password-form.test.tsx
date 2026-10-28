import "@testing-library/jest-dom/vitest";

import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderIntl } from "@/__tests__/intl-render";

import { ChangePasswordForm } from "./change-password-form";

afterEach(cleanup);

describe("ChangePasswordForm", () => {
  it("uses unique field IDs when two form instances are mounted", () => {
    renderIntl(
      <>
        <ChangePasswordForm onSuccess={vi.fn()} />
        <ChangePasswordForm onSuccess={vi.fn()} />
      </>,
    );

    const currentPasswordFields = screen.getAllByLabelText("当前密码");
    const newPasswordFields = screen.getAllByLabelText("新密码");
    const confirmPasswordFields = screen.getAllByLabelText("确认新密码");

    expect(new Set(currentPasswordFields.map((field) => field.id)).size).toBe(2);
    expect(new Set(newPasswordFields.map((field) => field.id)).size).toBe(2);
    expect(new Set(confirmPasswordFields.map((field) => field.id)).size).toBe(2);
  });

  it("supports an optional cancel action", () => {
    const onCancel = vi.fn();
    renderIntl(
      <ChangePasswordForm onSuccess={vi.fn()} onCancel={onCancel} />,
    );

    fireEvent.click(screen.getByRole("button", { name: "取消" }));

    expect(onCancel).toHaveBeenCalledOnce();
  });

  it("focuses the current password when requested", async () => {
    renderIntl(
      <ChangePasswordForm onSuccess={vi.fn()} autoFocusCurrent />,
    );

    await waitFor(() => {
      expect(screen.getByLabelText("当前密码")).toHaveFocus();
    });
  });
});
