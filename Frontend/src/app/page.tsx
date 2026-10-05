import type { Metadata } from "next";
import { Fragment, type ReactNode } from "react";

import {
  SAMPLE_BRANCH,
  SAMPLE_BRANCH_EVIDENCE,
  SAMPLE_BRIEF,
  SAMPLE_CHECKED_ON,
  SAMPLE_ISSUE_URL,
  SAMPLE_REPO,
  SAMPLE_SIGNALS,
} from "@/components/landing/sample";
import StartButton from "@/components/landing/start-button";
import StationIntake from "@/components/landing/station-intake";
import { ChakanaMark, Flag, Llama, LlamaTrail, Scribble } from "@/components/trail-art";

const DESCRIPTION =
  "Paste a GitHub issue. Camino checks whether it's up for grabs, finds the branch maintainers merge into, and writes a brief grounded in the code.";

export const metadata: Metadata = {
  title: "Camino — every issue has a trail",
  description: DESCRIPTION,
  openGraph: {
    title: "Camino — every issue has a trail",
    description: DESCRIPTION,
    type: "website",
  },
};

const REPO_URL = "https://github.com/jballo/camino";

function Station({
  ring,
  ringStyle = "default",
  eyebrow,
  title,
  children,
  art,
}: {
  ring: string;
  ringStyle?: "lit" | "default" | "dashed";
  eyebrow: string;
  title: string;
  children: ReactNode;
  art: ReactNode;
}) {
  const ringClass = {
    lit: "border-brand-accent bg-brand-accent text-background",
    default: "border-rail-dim bg-background text-brand-accent",
    dashed: "border-dashed border-rail-dim bg-background text-brand-accent",
  }[ringStyle];

  return (
    <article className="relative grid grid-cols-[52px_minmax(0,1fr)] gap-x-5 sm:gap-x-9 pb-[84px] last:pb-0 md:grid-cols-[52px_minmax(0,340px)_minmax(0,1fr)]">
      <span
        aria-hidden="true"
        className={`relative flex size-[52px] items-center justify-center rounded-full border-[2.5px] font-display text-[15px] ${ringClass}`}
      >
        {ring}
      </span>
      <div className="pt-1.5">
        <span className="eyebrow">{eyebrow}</span>
        <h2 className="mt-2 font-display text-[22px] uppercase leading-[1.15] tracking-[.02em]">
          {title}
        </h2>
        <p className="mt-2.5 text-[12.5px] leading-[18px] text-muted-foreground">{children}</p>
      </div>
      <div className="col-start-2 mt-5 min-w-0 md:col-start-auto md:mt-0">{art}</div>
    </article>
  );
}

/** Lets long paths and branch names wrap after `/`, `-` and `.`, not mid-word. */
function Breakable({ text }: { text: string }) {
  return text.split(/(?<=[/.-])/).map((part, index) => (
    <Fragment key={index}>
      {index > 0 && <wbr />}
      {part}
    </Fragment>
  ));
}

function Code({ children }: { children: ReactNode }) {
  return (
    <span className="rounded-[4px] border border-input bg-muted px-1.5 py-px font-mono text-[11px] text-foreground">
      {children}
    </span>
  );
}

function SignalsArt() {
  return (
    <div className="overflow-hidden rounded-[12px] border border-border bg-card">
      {SAMPLE_SIGNALS.map((issue) => (
        <div key={issue.number} className="flex flex-col gap-2.5 border-b border-border px-5 py-4">
          <div className="flex items-start justify-between gap-3">
            <p className="text-[13px] leading-[18px]">
              <span className="mr-1.5 font-mono text-[11px] text-muted-foreground">
                #{issue.number}
              </span>
              {issue.title}
            </p>
            <span
              className={`shrink-0 rounded-[4px] border px-2 py-0.5 font-mono text-[9.5px] uppercase tracking-[.14em] ${
                issue.verdict === "taken"
                  ? "border-warning/50 text-warning"
                  : "border-success/50 text-success"
              }`}
            >
              {issue.verdict === "taken" ? "Taken" : "Up for grabs"}
            </span>
          </div>
          <div className="flex flex-wrap gap-1.5">
            {issue.labels.map((label) => (
              <span
                key={label}
                className="rounded-[4px] border border-input px-2 py-0.5 font-mono text-[10px] text-muted-foreground"
              >
                {label}
              </span>
            ))}
          </div>
          {issue.warnings.length > 0 ? (
            <ul className="flex flex-col gap-1.5">
              {issue.warnings.map((warning) => (
                <li key={warning} className="flex gap-2 font-mono text-[10.5px] leading-[15px] text-warning">
                  <span aria-hidden="true">▲</span>
                  {warning}
                </li>
              ))}
            </ul>
          ) : (
            <p className="flex gap-2 font-mono text-[10.5px] text-success">
              <span aria-hidden="true">●</span>No warnings
            </p>
          )}
        </div>
      ))}
      <p className="px-5 py-3 font-mono text-[10px] text-muted-foreground">
        First pick was taken. The second was up for grabs.
      </p>
    </div>
  );
}

function BranchArt() {
  return (
    <div className="rounded-[12px] border border-border bg-card px-5 py-[18px]">
      <span className="eyebrow">PRs to this project target</span>
      <p className="mt-3 border-l-2 border-brand-accent pl-3.5 font-mono text-[13px] leading-[20px] text-brand-accent [overflow-wrap:anywhere]">
        <Breakable text={SAMPLE_BRANCH} />
      </p>
      <p className="mt-3 text-[12px] text-muted-foreground">{SAMPLE_BRANCH_EVIDENCE}</p>
    </div>
  );
}

function BriefArt() {
  return (
    <div className="rounded-[12px] border border-border bg-card px-5 py-[18px]">
      <span className="eyebrow">
        Brief · {SAMPLE_REPO} #{SAMPLE_BRIEF.number}
      </span>
      <h3 className="mt-2 text-[15px] font-medium">{SAMPLE_BRIEF.title}</h3>
      <p className="mt-2 text-[12.5px] leading-[18px] text-muted-foreground">{SAMPLE_BRIEF.summary}</p>
      <dl className="mt-4 flex flex-wrap gap-x-6 gap-y-2">
        {SAMPLE_BRIEF.counts.map((count) => (
          <div key={count.label}>
            <dt className="font-mono text-[9px] uppercase tracking-[.16em] text-muted-foreground">
              {count.label}
            </dt>
            <dd className="mt-1 font-mono text-[13px]">{count.value}</dd>
          </div>
        ))}
      </dl>
      <ul className="mt-4 flex flex-col gap-2.5 border-t border-border pt-4">
        {SAMPLE_BRIEF.steps.map((step) => (
          <li key={step.title} className="grid grid-cols-[48px_minmax(0,1fr)] gap-2 text-[12px] leading-[17px]">
            <span className="pt-px font-mono text-[9.5px] uppercase tracking-[.14em] text-brand-accent">
              {step.kind}
            </span>
            <span className="text-muted-foreground">
              {step.title}
              {"path" in step && (
                <span className="mt-0.5 block font-mono text-[10.5px] text-foreground [overflow-wrap:anywhere]">
                  <Breakable text={step.path} />
                </span>
              )}
            </span>
          </li>
        ))}
      </ul>
      <p className="mt-4 font-mono text-[10px] text-muted-foreground">
        Indexed at {SAMPLE_BRIEF.indexedAt} · head {SAMPLE_BRIEF.indexedAt}
      </p>
    </div>
  );
}

function ArrivalArt() {
  return (
    <div className="flex items-end gap-[18px] border-[1.5px] border-dashed border-dash bg-card p-[22px]">
      <div className="flex shrink-0 items-end gap-1.5">
        <Llama size={42} className="text-brand-accent" />
        <Flag size={34} className="text-muted-foreground" />
      </div>
      <p className="min-w-0 text-[12.5px] leading-[18px] text-muted-foreground">
        Open your pull request against
        <span className="mt-0.5 block font-mono text-[11px] text-foreground [overflow-wrap:anywhere]">
          <Breakable text={SAMPLE_BRANCH} />
        </span>
      </p>
    </div>
  );
}

export default function Landing() {
  return (
    <>
      <div className="mx-auto flex w-full max-w-[1080px] flex-col px-5 sm:px-8 lg:px-[66px]">
        <section className="flex flex-col items-center pt-16 text-center">
          <div className="flex items-center gap-[9px]">
            <ChakanaMark className="shrink-0 text-brand-accent" />
            <span className="eyebrow">Open-source contribution helper</span>
          </div>
          <h1 className="mt-[18px] font-shade text-[clamp(36px,8vw,86px)] uppercase leading-[1.04] tracking-[.01em]">
            <span className="block">Every issue</span>
            <span className="block">
              has a <span className="text-brand-accent">trail</span>
            </span>
          </h1>
          <Scribble className="mt-1 max-w-full text-foreground" />
          <p className="mt-5 max-w-[520px] text-[13.5px] leading-[20.25px] text-muted-foreground">
            Camino walks it with you — from a GitHub issue URL to a grounded plan you can open a
            pull request from. Four stations, no guesswork.
          </p>
          <div className="mt-[26px] flex flex-wrap justify-center gap-3">
            <StartButton large>Start your trail</StartButton>
            <a href="#trail" className="button-ghost min-h-[50px]">
              Walk the stations ↓
            </a>
          </div>
          <p className="eyebrow mt-4">Any public repository</p>
        </section>

        <LlamaTrail bob className="mt-14" />

        <section
          id="trail"
          aria-label="How Camino works"
          className="relative mt-[88px] scroll-mt-10"
        >
          <span
            aria-hidden="true"
            className="absolute bottom-0 left-[17px] top-0 w-[18px] border-x-[2.25px] border-rail-dim"
            style={{
              backgroundImage:
                "repeating-linear-gradient(180deg, transparent 0 24px, var(--rail-tie) 24px 26px)",
            }}
          />
          <Station
            ring="01"
            ringStyle="lit"
            eyebrow="Station one"
            title="Paste the issue"
            art={
              <div className="rounded-[12px] border border-border bg-card px-5 py-[18px]">
                <StationIntake placeholder={SAMPLE_ISSUE_URL} />
              </div>
            }
          >
            Any issue on any public repository. No setup, no install — just the URL you were
            already looking at.
          </Station>
          <Station ring="02" eyebrow="Station two" title="Read the signals" art={<SignalsArt />}>
            Before anything is generated, Camino checks whether an issue is actually up for grabs —
            so you don&apos;t spend a weekend on work someone already claimed.
          </Station>
          <Station ring="03" eyebrow="Station three" title="Find the right branch" art={<BranchArt />}>
            Many projects don&apos;t merge into <Code>main</Code>. Camino checks where recent pull
            requests actually landed and shows you the evidence.
          </Station>
          <Station ring="04" eyebrow="Station four" title="Get the brief" art={<BriefArt />}>
            A plan grounded in the actual code: which files to read, how to set up, what to test,
            and what to ask the maintainer.
          </Station>
          <Station
            ring="PR"
            ringStyle="dashed"
            eyebrow="Arrival"
            title="Open your pull request"
            art={<ArrivalArt />}
          >
            The last stretch is yours. Camino keeps the brief so you can come back to it.
          </Station>
        </section>

        <p className="mt-14 text-center font-mono text-[10px] leading-[16px] text-muted-foreground">
          Stations 2–4 show real Camino output for {SAMPLE_REPO} #322 and #316, checked{" "}
          {SAMPLE_CHECKED_ON}. The other contributor&apos;s name is removed.
        </p>

        <section className="mt-[110px] flex flex-col items-center text-center">
          <h2 className="font-shade text-[clamp(30px,5.6vw,60px)] uppercase leading-[1.04] tracking-[.01em]">
            <span className="block">Your llama</span>
            <span className="block">
              is <span className="text-brand-accent">waiting</span>
            </span>
          </h2>
          <div className="mt-7">
            <StartButton large>Sign in to start</StartButton>
          </div>
        </section>
      </div>

      <footer className="mt-24 border-t border-border">
        <div className="mx-auto flex min-h-[84px] w-full max-w-[1080px] flex-wrap items-center justify-between gap-5 px-5 py-6 sm:px-8 lg:px-[66px]">
          <span className="font-display text-[15px] uppercase leading-none">Camino</span>
          <span className="font-mono text-[9px] text-muted-foreground">Public repositories only</span>
          <a
            href={REPO_URL}
            target="_blank"
            rel="noreferrer"
            className="font-mono text-[9.5px] uppercase tracking-[.18em] text-muted-foreground transition hover:text-foreground"
          >
            GitHub
          </a>
        </div>
      </footer>
    </>
  );
}
