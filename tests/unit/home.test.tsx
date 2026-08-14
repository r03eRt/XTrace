import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import HomePage from "@/app/page";

describe("HomePage", () => {
  it("renderiza el título con data-testid estable (UX-001)", () => {
    render(<HomePage />);
    expect(screen.getByTestId("home-title")).toHaveTextContent("Proyect-skeleton");
  });
});
