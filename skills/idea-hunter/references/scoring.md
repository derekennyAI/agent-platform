# Idea Scoring Rubric

Score each dimension 1–10. Final score = weighted average.

## Weights

| Dimension | Weight | What it measures |
|-----------|--------|------------------|
| Demand | 0.25 | How many people have this pain? How vocal are they? |
| Simplicity | 0.25 | Can a solo dev ship an MVP in 2–4 weeks? |
| Timing | 0.20 | Is there a recent trigger (new API, regulation, trend)? |
| Moat | 0.15 | Can you build a defensible advantage (data, network, niche)? |
| Founder Fit | 0.15 | Does it match Farlen's skills, interests, and resources? |

## Scoring Guide

### Demand (weight: 0.25)
- **9–10**: Hundreds of complaints, multiple Reddit threads, people paying for hacky workarounds
- **7–8**: Clear repeated pain in multiple communities, some existing solutions but gaps remain
- **5–6**: Moderate interest, a few threads, "nice to have" territory
- **3–4**: Niche problem, small audience, unclear if people would pay
- **1–2**: Solution looking for a problem

### Simplicity (weight: 0.25)
- **9–10**: Static site or simple CRUD app, one API, can ship in a weekend
- **7–8**: Straightforward SaaS, well-understood stack, 2–4 week MVP
- **5–6**: Moderate complexity, needs 1–2 integrations, some learning curve
- **3–4**: Significant engineering, needs infra, multiple moving parts
- **1–2**: Requires ML pipeline, massive data, regulatory compliance, or hardware

### Timing (weight: 0.20)
- **9–10**: New platform/API just launched, regulation just passed, trend is accelerating
- **7–8**: Growing trend, early but clear momentum
- **5–6**: Steady market, no particular urgency
- **3–4**: Market is mature, incumbents entrenched
- **1–2**: Declining interest, already solved well

### Moat (weight: 0.15)
- **9–10**: Strong network effects, proprietary data, or regulatory barrier
- **7–8**: Data advantage grows over time, niche expertise required
- **5–6**: Some switching costs, brand/community possible
- **3–4**: Easy to copy, commodity features
- **1–2**: No differentiation, pure execution race

### Founder Fit (weight: 0.15)
- **9–10**: Directly solves a problem Farlen has, domain expertise, personal passion
- **7–8**: Adjacent to existing skills/interests, enjoyable to work on
- **5–6**: Learnable domain, moderate interest
- **3–4**: Unfamiliar territory, would need significant ramp-up
- **1–2**: No interest or connection, wrong skill set entirely

## Formula

```
total = demand*0.25 + simplicity*0.25 + timing*0.20 + moat*0.15 + founder_fit*0.15
```

## Quick Interpretation

| Score | Verdict |
|-------|---------|
| 8.0+ | Strong candidate — investigate immediately |
| 6.5–7.9 | Promising — worth a deeper look |
| 5.0–6.4 | Maybe — needs a unique angle |
| < 5.0 | Pass — move on |
