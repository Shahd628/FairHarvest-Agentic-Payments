# CLAUDE.md - Building Farmi: a Potato Sales Negotiation Agent (Blockchain)

> Automatically read by Claude code every session.
> Tells Claude what the project is, how to code, and which directives to follow.

## Project Overview

**Buildng Farmi: Blockchain-integrated Sales Negotiation Agent** - You will build an AI agent that can autonomously:
1. Poll a potato-exchange contract's logs (on-chain events) for offers
2. Initiate the offer's negotiation process if buyer is not blacklisted
3. Check a different oracle contract for latest price update
4. Counteroffer or accept deal according to farmer policy
5. Expire deal if exchange criteria not met

## Rules

- Always ask clarifying questions before implementing
- Show your methodology and explain why a design decision is made
- Create a new wallet for the agent to create transactions
- Flag any security or unexpected issues in the agentic workflow
- Never store secrets, API keys, or passwords in code. Only store them in `.env`
- Use environment variables for sensitive configuration

## Tech Stack

- Blockchain network: Sepolia ETH testnet
- Blockchain RPC: Infura (token provided in .env file)
- Language: Python 3

## Folder Structure

- .gitignore -- ensure `.env` is declared here
- .env -- provided infura token and new agent wallet private key stored here
- workflows/ - Workflow instruction files
- output/ - Finished deliverables
- resources/ - Reference docs and templates