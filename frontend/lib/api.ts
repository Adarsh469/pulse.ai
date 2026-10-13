import {
    ResetResult,
    StepResult,
    TriageAction,
    TaskMeta,
} from "./types";
import { ExplainData } from "@/components/ExplanationPanel";

// In Docker: browser calls /api/* → Next.js server rewrites to http://backend:7860/*
// In local dev: use NEXT_PUBLIC_API_URL (default http://localhost:7860) directly
const API_URL =
    process.env.NODE_ENV === "production"
        ? "/api"
        : (process.env.NEXT_PUBLIC_API_URL || "http://localhost:7860");

async function api<T>(
    path: string,
    options?: RequestInit
): Promise<T> {
    const res = await fetch(`${API_URL}${path}`, {
        headers: { "Content-Type": "application/json" },
        ...options,
    });
    if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail ?? "API error");
    }
    return res.json() as Promise<T>;
}

export const triageApi = {
    health: () => api<{ status: string; version: string }>("/health"),

    tasks: () => api<{ tasks: TaskMeta[] }>("/tasks"),

    reset: (taskId: string, sessionId = "default", seed?: number) =>
        api<ResetResult>("/reset", {
            method: "POST",
            body: JSON.stringify({ task_id: taskId, session_id: sessionId, seed }),
        }),

    step: (action: TriageAction) =>
        api<StepResult & { info?: Record<string, unknown> }>("/step", {
            method: "POST",
            body: JSON.stringify(action),
        }),

    state: (sessionId = "default") =>
        api<Record<string, unknown>>(`/state?session_id=${sessionId}`),

    explain: (sessionId = "default") =>
        api<ExplainData>(`/explain?session_id=${sessionId}`),

    learnedHeuristics: () =>
        api<{ heuristics: Record<string, { avg_esi: number; count: number; correct_rate: number }> }>("/learned_heuristics"),

    feedbackStats: () =>
        api<{ total: number; avg_reward: number | null }>("/feedback/stats"),

    submitFeedback: (payload: {
        case_id: string;
        task_id: string;
        symptoms: string[];
        human_esi?: number;
        reward?: number;
    }) =>
        api<{ status: string }>("/feedback", {
            method: "POST",
            body: JSON.stringify({ ...payload, session_id: "frontend-user" }),
        }),
};
