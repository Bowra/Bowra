import { describe, expect, it } from "vitest";

import { detectCjkContentLang } from "./content-lang";

describe("detectCjkContentLang", () => {
  it("marks Chinese prose with product names and numbers as Chinese", () => {
    expect(detectCjkContentLang("华东 Q3 GMV 同比下降 8,742 万元（-8.4%），主要拖累集中在江苏外套品类。")).toBe("zh");
  });

  it("leaves Latin answers unmarked, even when they quote a few Chinese terms", () => {
    expect(detectCjkContentLang("Revenue in the East region (华东) fell 8.4% year over year, mostly in outerwear.")).toBeNull();
    expect(detectCjkContentLang("Der Umsatz in der Region Ost sank um 8,4 %.")).toBeNull();
    expect(detectCjkContentLang("")).toBeNull();
    expect(detectCjkContentLang(undefined)).toBeNull();
  });

  it("tells Japanese and Korean apart from Chinese", () => {
    expect(detectCjkContentLang("東日本の売上は前年比で8.4%減少しました。")).toBe("ja");
    expect(detectCjkContentLang("동부 지역 매출은 전년 대비 8.4% 감소했습니다.")).toBe("ko");
  });
});
