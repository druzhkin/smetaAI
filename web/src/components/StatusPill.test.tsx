import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StatusPill, statusTone } from "./StatusPill";

describe("StatusPill", () => {
  it("renders BLOCKED as a negative control state", () => {
    render(<StatusPill value="BLOCKED" />);
    expect(screen.getByText("Заблокирован")).toBeInTheDocument();
    expect(statusTone("BLOCKED")).toBe("negative");
  });

  it("does not describe an unknown state as approved", () => {
    render(<StatusPill value="UNRECOGNISED_STATE" />);
    expect(screen.getByText("UNRECOGNISED STATE")).toBeInTheDocument();
    expect(statusTone("UNRECOGNISED_STATE")).toBe("neutral");
  });

  it("does not present a clean malware scan as completed processing", () => {
    render(<StatusPill value="CLEAN" />);
    expect(
      screen.getByText("Malware scan: угроз не выявлено"),
    ).toBeInTheDocument();
    expect(statusTone("CLEAN")).toBe("warning");
    expect(statusTone("SCAN_FAILED")).toBe("negative");
    expect(statusTone("PROCESSING_FAILED")).toBe("negative");
  });
});
