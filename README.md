# Agentic Onboarding Lab

A reference implementation for **AI-assisted customer onboarding** where an agent coordinates evidence collection, validation, next-best-action selection and human escalation.

The goal is not to automate every decision. The goal is to make the onboarding service faster while keeping decision authority, evidence provenance and escalation policy explicit.

## Customer journey

```mermaid
journey
    title AI-assisted onboarding
    section Start
      Choose product: 5: Customer
      Give consent: 5: Customer
    section Identity
      Upload / capture ID: 4: Customer
      Extract fields: 5: Agent
      Validate identity evidence: 5: Agent
    section Profile
      Ask only missing questions: 4: Agent, Customer
      Validate address and occupation: 4: Agent
    section Risk checks
      Run deterministic checks: 5: System
      Build evidence packet: 5: Agent
      Escalate ambiguity: 4: Human reviewer
    section Finish
      Explain outcome: 5: Agent, Human reviewer
      Record audit event: 5: System
```

## State machine

```mermaid
stateDiagram-v2
    [*] --> Started
    Started --> ConsentCaptured
    ConsentCaptured --> IdentityPending
    IdentityPending --> IdentityVerified: evidence valid
    IdentityPending --> NeedsCustomerInput: missing / unreadable evidence
    NeedsCustomerInput --> IdentityPending: customer responds
    IdentityVerified --> ProfilePending
    ProfilePending --> RiskScreening
    RiskScreening --> HumanReview: uncertainty / policy trigger
    RiskScreening --> Approved: low risk + sufficient evidence
    HumanReview --> NeedsCustomerInput: reviewer requests evidence
    HumanReview --> Approved
    HumanReview --> Rejected
    Approved --> Completed
    Rejected --> Completed
    Completed --> [*]
```

## Agent responsibilities

The onboarding agent can:

- identify missing information;
- choose the next best question;
- normalize evidence into structured fields;
- execute allowed validation tools;
- summarize evidence for a reviewer;
- recommend a route;
- generate customer-facing status messages;
- persist an audit trail.

The agent **cannot** silently override mandatory-review policy, delete evidence, weaken validation rules or convert uncertainty into an automatic approval.

## Architecture

```mermaid
flowchart TB
    UI[Customer onboarding UI] --> ORCH[Onboarding orchestrator]
    ORCH --> STATE[(Case state store)]
    ORCH --> AGENT[Agent planner]
    AGENT --> TOOLS[Allowed tools]
    TOOLS --> ID[Identity validation]
    TOOLS --> REG[Registry / profile checks]
    TOOLS --> DOC[Document extraction]
    AGENT --> EVID[(Evidence ledger)]
    EVID --> POLICY[Policy router]
    POLICY -->|automate| RESULT[Outcome]
    POLICY -->|ask customer| UI
    POLICY -->|escalate| REVIEW[Human reviewer]
    REVIEW --> RESULT
    RESULT --> AUDIT[(Audit events)]
    RESULT --> OBS[Journey + service metrics]
```

## Important service-design metrics

- completion rate;
- average onboarding time;
- abandonment by journey stage;
- percentage of questions skipped because data already exists;
- manual-review rate;
- agent/human disagreement rate;
- evidence-request loops per case;
- percentage of cases with actionable explanations;
- SLA breach rate;
- automation rate by impact tier.

## Repository layout

```text
agentic-onboarding-lab/
├── onboarding.py       # state machine, evidence and orchestration
├── onboarding_ui.html  # static customer journey prototype
└── README.md
```

## Demo

```bash
python onboarding.py
```

All demo identities and evidence are synthetic. This project contains no real customer or employer information.
