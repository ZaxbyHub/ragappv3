import type { MemoryResult, MemoryWikiStatus } from "@/lib/api";

export const mockMemories: MemoryResult[] = import.meta.env.DEV ? [
  {
    id: "mem-101",
    content: "User prefers concise answers with bullet points for technical questions.",
    metadata: { category: "preference", tags: ["user-style", "communication"], source: "chat-feedback" },
    score: 0.95,
  },
  {
    id: "mem-102",
    content: "The production API base URL is https://api.example.com/v2.",
    metadata: { category: "fact", tags: ["api", "infrastructure"], source: "documentation" },
    score: 0.92,
  },
  {
    id: "mem-103",
    content: "Team standups are held every Tuesday and Thursday at 10:00 AM UTC.",
    metadata: { category: "schedule", tags: ["team", "meetings"], source: "onboarding-doc" },
    score: 0.88,
  },
  {
    id: "mem-104",
    content: "The vector database uses cosine similarity with a default threshold of 0.75.",
    metadata: { category: "configuration", tags: ["search", "database"], source: "settings" },
    score: 0.97,
  },
  {
    id: "mem-105",
    content: "Preferred LLM model for summarization tasks is llama-3-70b-instruct.",
    metadata: { category: "preference", tags: ["ai", "models"], source: "chat-history" },
    score: 0.91,
  },
  {
    id: "mem-106",
    content: "All customer data must be encrypted at rest using AES-256.",
    metadata: { category: "policy", tags: ["security", "compliance"], source: "security-audit-2023" },
    score: 0.99,
  },
  {
    id: "mem-107",
    content: "The staging environment is located at staging.internal.example.com.",
    metadata: { category: "infrastructure", tags: ["ops", "urls"], source: "runbook" },
    score: 0.89,
  },
  {
    id: "mem-108",
    content: "Release process requires two approvals and a green CI pipeline before deployment.",
    metadata: { category: "procedure", tags: ["devops", "release"], source: "wiki-page" },
    score: 0.93,
  },
  {
    id: "mem-109",
    content: "Chunk overlap is set to 200 characters for better context continuity during retrieval.",
    metadata: { category: "configuration", tags: ["search", "chunking"], source: "settings" },
    score: 0.94,
  },
  {
    id: "mem-110",
    content: "User interface should follow the established design system with Tailwind CSS and shadcn/ui components.",
    metadata: { category: "guideline", tags: ["ui", "design"], source: "design-doc" },
    score: 0.90,
  },
] : [];

export const mockMemoryWikiStatuses: Record<string, MemoryWikiStatus> = import.meta.env.DEV ? ({
  "mem-101": {
    memory_id: 101,
    wiki_status: "promoted",
    claims_count: 2,
    active_claims: 2,
    stale_claims: 0,
    linked_pages: [
      { id: 10, slug: "user-preferences", title: "User Preferences", page_type: "entity", status: "verified" },
    ],
    latest_job: {
      id: 20,
      vault_id: 1,
      trigger_type: "memory",
      trigger_id: "mem-101",
      status: "completed",
      error: null,
      result_json: "{}",
      created_at: "2024-05-01T10:00:00Z",
      started_at: "2024-05-01T10:00:05Z",
      completed_at: "2024-05-01T10:01:00Z",
      retry_count: 0,
    },
    job_count: 1,
  },
  "mem-106": {
    memory_id: 106,
    wiki_status: "promoted",
    claims_count: 1,
    active_claims: 1,
    stale_claims: 0,
    linked_pages: [
      { id: 11, slug: "data-encryption", title: "Data Encryption Policy", page_type: "procedure", status: "verified" },
    ],
    latest_job: null,
    job_count: 0,
  },
}) : ({} as Record<string, MemoryWikiStatus>);
