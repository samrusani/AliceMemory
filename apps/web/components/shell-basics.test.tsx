import React from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AppShell } from "./app-shell";
import { GmailAccountDetail } from "./gmail-account-detail";
import { HomePageContent } from "./home-page-content";
import { WorkspaceLoading } from "./workspace-loading";

vi.mock("next/link", () => ({
  default: ({ href, children, className, "aria-current": ariaCurrent }: {
    href: string; children: React.ReactNode; className?: string; "aria-current"?: "page";
  }) => <a href={href} className={className} aria-current={ariaCurrent}>{children}</a>,
}));
vi.mock("next/navigation", () => ({ usePathname: () => "/continuity" }));

describe("shell coverage", () => {
  afterEach(cleanup);

  it("renders exactly the seven default views and no removed or legacy links", () => {
    render(<HomePageContent legacyEnabled={false} />);
    expect(screen.getByRole("heading", { name: "Continuity and memory review console" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Artifact Review/i })).toHaveAttribute("href", "/artifacts");
    expect(screen.getByRole("link", { name: /Continuity Workspace/i })).toHaveAttribute("href", "/continuity");
    expect(screen.getByText("7")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Legacy Approval Inbox/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Hosted/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Chief-of-Staff/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Chat/i })).not.toBeInTheDocument();
    expect(screen.getByText("Narrow by design")).toBeInTheDocument();
  });

  it("marks the current navigation item and exposes the skip target", () => {
    render(<AppShell legacySurfacesEnabled={false}><p>content</p></AppShell>);
    const continuityLinks = screen.getAllByRole("link", { name: /Continuity\s*Capture, recall/i });
    expect(continuityLinks).toHaveLength(2);
    expect(continuityLinks.every((link) => link.getAttribute("aria-current") === "page")).toBe(true);
    expect(screen.getAllByRole("link", { name: /Continuity\s*Capture, recall/i })).toHaveLength(2);
    expect(screen.queryByRole("link", { name: /Gmail\s*Legacy manual/i })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Skip to main content" })).toHaveAttribute("href", "#main-content");
  });

  it("renders the four compatibility links only from the resolved flag", () => {
    const { rerender } = render(<HomePageContent legacyEnabled />);
    expect(screen.getByRole("link", { name: /Legacy Approval Inbox/i })).toHaveAttribute(
      "href",
      "/approvals",
    );
    expect(screen.getByRole("link", { name: /Legacy Task Inspection/i })).toHaveAttribute(
      "href",
      "/tasks",
    );
    expect(screen.getByRole("link", { name: /Legacy Gmail Review/i })).toHaveAttribute(
      "href",
      "/gmail",
    );
    expect(screen.getByRole("link", { name: /Legacy Calendar Review/i })).toHaveAttribute(
      "href",
      "/calendar",
    );

    rerender(<AppShell legacySurfacesEnabled><p>content</p></AppShell>);
    expect(screen.getAllByRole("link", { name: /Approvals\s*Legacy approval queue/i })).toHaveLength(2);
    expect(screen.getAllByRole("link", { name: /Calendar\s*Legacy manual account/i })).toHaveLength(2);
  });

  it("renders bounded loading state and unavailable Gmail detail", () => {
    const { rerender } = render(
      <WorkspaceLoading eyebrow="Gmail" title="Loading Gmail" description="Resolving accounts" />,
    );
    expect(screen.getByText("Loading route state")).toBeInTheDocument();
    expect(screen.getByText("Primary workspace")).toBeInTheDocument();

    rerender(<GmailAccountDetail account={null} source="unavailable" unavailableReason="backend down" />);
    expect(screen.getByText("Account detail unavailable")).toBeInTheDocument();
    expect(screen.getByText("backend down")).toBeInTheDocument();
  });
});
