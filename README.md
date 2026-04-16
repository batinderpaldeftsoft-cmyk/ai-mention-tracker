# AI Mention Tracker

Automated monitoring of brand mentions across major AI platforms (Google AI Mode, ChatGPT, Gemini, Perplexity, Claude).

## Features
- **Real-time Keyword Tracking**: Monitor specific terms and discover brand mentions.
- **Deep Discovery**: Reverse lookup to find where your brand is already cited.
- **Cloud Powered**: Hosted on Vercel with Postgres storage.

## Deployment
This repository is configured for one-click deployment to Vercel.
- Uses `vercel.json` for serverless function routing.
- Uses `psycopg2` for Vercel Postgres integration.
