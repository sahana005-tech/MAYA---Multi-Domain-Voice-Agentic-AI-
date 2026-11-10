# Adobe Acrobat Zero-Day Analysis

Reverse-engineering notes and supporting material for an analysis of the Adobe Acrobat JavaScript trust model and its relation to the reported zero-day abuse path involving privileged Acrobat APIs.

## Overview

This repository contains a technical analysis of how Adobe Acrobat exposes privileged JavaScript functionality through native handlers, how those handlers are gated, and why the observed issue is best understood as a **trust/context handling flaw** rather than a simple missing access check.

The analysis focuses on the recovered execution path for:

- `util.readFileIntoStream`
- `app.trustedFunction`
- `app.beginPriv`
- related trust and context-gating helpers

The core conclusion is that the vulnerable behavior is most consistent with **untrusted document JavaScript being treated as if it were executing in a privileged context**.

## Main Findings

The reverse engineering shows the following:

- `util.readFileIntoStream` is exported through the Acrobat JavaScript layer to a native handler
- the native handler parses attacker-controlled inputs such as file path and encoding options
- access is not unconditional; it is gated by privileged execution contexts such as `Batch`, `Exec`, and `Console`
- `app.trustedFunction` is also gated and constructs trusted wrappers only under allowed conditions
- `app.beginPriv` is separately protected by a gate predicate before privileged mode is entered
- several privileged paths converge on a shared denial routine

This structure suggests that exploitation does **not** rely on the privileged APIs being totally unprotected, it more likely relies on incorrect trust propagation, context confusion, or misclassification of the current execution frame.

## Repository Goal

This repository is intended to document:

- architectural evidence from native reverse engineering
- the JavaScript-to-native handler mapping
- the trust-wrapper and privilege-entry mechanisms
- why the observed behavior aligns with a logic flaw in the Acrobat trust boundary

## Document Structure

The paper includes:

1. threat and vulnerability summary
2. recovered native handler mappings
3. analysis of `util.readFileIntoStream`
4. analysis of `app.trustedFunction`
5. analysis of `app.beginPriv`
6. interpretation of the likely logic flaw
7. appendix material with graphs and annotated listings

## Evidence Highlights

Key recovered mappings include:

- `util.readFileIntoStream -> sub_7F9EF`
- `app.trustedFunction -> sub_77AF5`
- `app.beginPriv -> sub_7801F`
- `app.endPriv -> sub_780BB`
- `app.isTrusted -> sub_78BF3`

The paper also discusses:

- argument parsing for `cDIPath` and `bEncodeBase64`
- context resolution for `Batch`, `Exec`, `Console`, `App`, `Init`, `Internal`, `External`
- centralized denial behavior via a shared error path
- file-backed object creation on successful privileged access
