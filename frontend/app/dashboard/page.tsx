"use client";

import { useState, useCallback, useEffect } from "react";
import Link from "next/link";
import {
    ShieldPlusIcon, PlayIcon, UserIcon, BotIcon,
    RefreshCwIcon, ChevronRightIcon, TrophyIcon, AlertCircleIcon,
    BrainCircuitIcon, UsersIcon,
} from "lucide-react";
import { triageApi } from "@/lib/api";
import { TaskId, PatientSummary, TriageObservation, QuestionTopic } from "@/lib/types";
import { getPatientName } from "@/lib/names";
import { PatientCard, ESIBadge } from "@/components/PatientCard";
import { ESISelector } from "@/components/ESISelector";
import { QueueReorder } from "@/components/QueueReorder";
import { DoctorChat } from "@/components/DoctorChat";
import { ExplanationPanel, ExplainData } from "@/components/ExplanationPanel";
import { AnimatedQueue } from "@/components/AnimatedQueue";
import { AgentChat, ChatMessage } from "@/components/AgentChat";
import { Logo } from "@/components/Logo";

// ─── constants ────────────────────────────────────────────────────────────────

const TASKS: { id: TaskId; label: string; badge: string; color: string }[] = [
    { id: "task1_esi_assignment", label: "ESI Assignment", badge: "Easy", color: "#30d158" },
    { id: "task2_queue_priority", label: "Queue Priority", badge: "Medium", color: "#ffd60a" },
    { id: "task3_ambiguous_triage", label: "Ambiguous Triage", badge: "Hard", color: "#ff6b00" },
];

const SESSION = "frontend-user";


// ─── learning helpers ─────────────────────────────────────────────────────────

async function fetchLearnedHeuristics(): Promise<Record<string, { avg_esi: number; count: number; correct_rate: number }>> {
    try {
        const data = await triageApi.learnedHeuristics();
        return data.heuristics ?? {};
    } catch { return {}; }
}

// ─── AI agent runner ──────────────────────────────────────────────────────────

async function runAIAgent(
    taskId: TaskId,
    onObsUpdate: (obs: TriageObservation) => void,
    onQueueSorted: (initial: PatientSummary[], final: PatientSummary[]) => void,
    onChatMessage: (msg: ChatMessage) => void,
    onDone: (score: number, finalObs: TriageObservation) => void,
) {
    const learnedHeuristics = await fetchLearnedHeuristics();

    // Single clean reset
    const resetResult = await triageApi.reset(taskId, SESSION + "-ai");
    let currentObs: TriageObservation = resetResult.observation;
    onObsUpdate(currentObs);

    // For Task 2: capture initial order for animation
    const initialQueue = currentObs.queue ? [...currentObs.queue] : [];

    let totalReward = 0;
    let lastAssignedEsi: number | null = null; // track for feedback submission

    for (let step = 1; step <= 10; step++) {
        await new Promise((r) => setTimeout(r, 900));

        let action: Parameters<typeof triageApi.step>[0];

        if (taskId === "task1_esi_assignment") {
            const symptoms = currentObs.patient?.symptoms ?? [];
            let esiVotes: number[] = [];
            for (const sym of symptoms) {
                const h = learnedHeuristics[sym];
                if (h && h.count >= 2 && h.correct_rate >= 0.6) esiVotes.push(Math.round(h.avg_esi));
            }
            let esiLevel: number;
            if (esiVotes.length > 0) {
                esiLevel = Math.round(esiVotes.reduce((a, b) => a + b, 0) / esiVotes.length);
            } else {
                const critical = symptoms.some((s) => ["chest pain", "shortness of breath", "blurred vision", "altered mental status"].includes(s));
                const high = symptoms.some((s) => ["severe headache", "syncope", "difficulty breathing"].includes(s));
                esiLevel = critical ? (symptoms.length >= 3 ? 1 : 2) : high ? 2 : symptoms.length >= 3 ? 3 : 4;
            }
            action = { action_type: "assign_esi", esi_level: esiLevel, session_id: SESSION + "-ai" };
            lastAssignedEsi = esiLevel;

        } else if (taskId === "task2_queue_priority") {
            const queue = (currentObs.queue ?? []).slice();
            const criticalFlags = new Set(["chest pain", "shortness of breath", "blurred vision", "altered mental status"]);
            const highFlags = new Set(["severe headache", "syncope", "difficulty breathing"]);
            const score = (p: PatientSummary) =>
                p.symptoms.filter((s) => criticalFlags.has(s)).length * 20 +
                p.symptoms.filter((s) => highFlags.has(s)).length * 10 +
                p.symptom_count;
            const sorted = [...queue].sort((a, b) => score(b) - score(a));
            action = { action_type: "reorder_queue", queue_order: sorted.map((p) => p.case_id), session_id: SESSION + "-ai" };
            // Trigger animation immediately
            onQueueSorted(initialQueue, sorted);

        } else {
            // Task 3: ask questions then assign
            const budget = currentObs.clarification_budget ?? 0;
            if (budget > 0 && !currentObs.awaiting_final_esi) {
                const topics: QuestionTopic[] = ["medications", "allergies", "current_symptoms"];
                const topic = topics[Math.min(3 - budget, topics.length - 1)];
                action = { action_type: "ask_question", question_topic: topic, session_id: SESSION + "-ai" };
                onChatMessage({ from: "agent", topic, text: `Consulting doctor about: ${topic.replace(/_/g, " ")}` });
            } else {
                const symptoms = currentObs.patient?.symptoms ?? [];
                const critical = symptoms.some((s) => ["chest pain", "shortness of breath", "blurred vision"].includes(s));
                const msgLower = (currentObs.message ?? "").toLowerCase();
                const hasContra = ["warfarin", "anticoagulant", "clopidogrel", "insulin glargine", "prednisone"].some((k) => msgLower.includes(k));
                action = { action_type: "assign_esi", esi_level: (critical || hasContra) ? 1 : critical ? 2 : 3, session_id: SESSION + "-ai" };
                lastAssignedEsi = action.esi_level as number;
            }
        }

        try {
            const result = await triageApi.step(action);
            totalReward = result.reward;
            currentObs = result.observation;
            onObsUpdate(currentObs);

            // For Task 3, capture doctor reply
            if (taskId === "task3_ambiguous_triage" && action.action_type === "ask_question" && result.observation.message) {
                onChatMessage({ from: "doctor", text: result.observation.message, timestamp: Date.now() });
            }

            if (result.done) break;
        } catch {
            break;
        }
    }

    // ── Submit feedback for learning (same as human mode) ──────────────────────
    try {
        if ((taskId === "task1_esi_assignment" || taskId === "task3_ambiguous_triage")
            && currentObs.patient && lastAssignedEsi !== null) {
            await triageApi.submitFeedback({
                case_id: currentObs.patient.case_id,
                task_id: taskId,
                symptoms: currentObs.patient.symptoms,
                human_esi: lastAssignedEsi,
                reward: totalReward,
            });
        }
        // Task 2: queue ordering, no per-patient ESI assignment to record
    } catch { /* non-critical */ }

    onDone(totalReward, currentObs);
}

// ─── Dashboard ────────────────────────────────────────────────────────────────

export default function DashboardPage() {
    const [taskId, setTaskId] = useState<TaskId>("task1_esi_assignment");
    const [mode, setMode] = useState<"human" | "ai">("human");

    // Episode state
    const [obs, setObs] = useState<TriageObservation | null>(null);
    const [loading, setLoading] = useState(false);
    const [selectedESI, setSelectedESI] = useState<number | null>(null);
    const [orderedQueue, setOrderedQueue] = useState<PatientSummary[]>([]);

    // Result
    const [episodeDone, setEpisodeDone] = useState(false);
    const [score, setScore] = useState<number | null>(null);
    const [explanation, setExplanation] = useState<ExplainData | null>(null);

    // Human action tracking
    const [humanActions, setHumanActions] = useState<string[]>([]);
    const [humanESI, setHumanESI] = useState<number | null>(null);
    const [humanQueueOrder, setHumanQueueOrder] = useState<string[]>([]);

    // AI agent state
    const [agentRunning, setAgentRunning] = useState(false);
    const [agentChatMessages, setAgentChatMessages] = useState<ChatMessage[]>([]);

    // Task 2 animation state
    const [initialQueueForAnim, setInitialQueueForAnim] = useState<PatientSummary[]>([]);
    const [sortedQueue, setSortedQueue] = useState<PatientSummary[]>([]);
    const [isQueueAnimating, setIsQueueAnimating] = useState(false);

    const [error, setError] = useState<string | null>(null);
    const [stats, setStats] = useState<{ total: number; avg_reward: number | null }>({ total: 0, avg_reward: null });

    const addChatMsg = useCallback((msg: ChatMessage) => setAgentChatMessages((prev) => [...prev, msg]), []);

    useEffect(() => {
        triageApi.feedbackStats().then(setStats).catch(() => { });
    }, [episodeDone]);

    const resetEpisodeState = () => {
        setEpisodeDone(false);
        setScore(null);
        setExplanation(null);
        setSelectedESI(null);
        setError(null);
        setAgentRunning(false);
        setAgentChatMessages([]);
        setHumanActions([]);
        setHumanESI(null);
        setHumanQueueOrder([]);
        setIsQueueAnimating(false);
        setSortedQueue([]);
    };

    // ── Fetch explanation after episode ─────────────────────────────────────────
    const fetchExplanation = async (sessionId: string, finalReward?: number) => {
        try {
            const data = await triageApi.explain(sessionId);
            setExplanation(data);

            // Task 2: use true_esi from explanation to submit per-patient feedback.
            // This is the only source of correct ESI for queue patients, and gives us
            // 5 learning data points per run (vs 1 for Tasks 1/3).
            if (data.task_id === "task2_queue_priority" && data.correct_order) {
                for (const p of data.correct_order) {
                    triageApi.submitFeedback({
                        case_id: p.case_id,
                        task_id: data.task_id,
                        symptoms: p.symptoms,
                        human_esi: p.true_esi,
                        reward: finalReward ?? 0,
                    }).catch(() => { });
                }
            }
        } catch { /* non-critical */ }
    };

    // ── Human: start ─────────────────────────────────────────────────────────
    const handleReset = async () => {
        setLoading(true);
        resetEpisodeState();
        setObs(null);
        try {
            const res = await triageApi.reset(taskId, SESSION);
            setObs(res.observation);
            if (res.observation.queue) setOrderedQueue(res.observation.queue);
        } catch (e: unknown) { setError(String(e)); }
        finally { setLoading(false); }
    };

    // ── Human: submit ────────────────────────────────────────────────────────
    const handleHumanSubmit = async () => {
        if (!obs) return;
        setLoading(true);
        setError(null);
        try {
            let action: Parameters<typeof triageApi.step>[0];
            if (taskId === "task1_esi_assignment" && selectedESI) {
                action = { action_type: "assign_esi", esi_level: selectedESI, session_id: SESSION };
                setHumanActions((prev) => [...prev, `Assigned ESI ${selectedESI}`]);
                setHumanESI(selectedESI);
            } else if (taskId === "task2_queue_priority") {
                action = { action_type: "reorder_queue", queue_order: orderedQueue.map((p) => p.case_id), session_id: SESSION };
                setHumanQueueOrder(orderedQueue.map((p) => p.case_id));
                setHumanActions((prev) => [...prev, `Submitted queue: ${orderedQueue.map((p) => getPatientName(p.case_id, p.gender)).join(" → ")}`]);
            } else {
                if (!selectedESI) { setLoading(false); return; }
                action = { action_type: "assign_esi", esi_level: selectedESI, session_id: SESSION };
                setHumanESI(selectedESI);
            }

            const res = await triageApi.step(action);
            setObs(res.observation);

            if (res.done) {
                setEpisodeDone(true);
                setScore(res.reward);
                await fetchExplanation(SESSION, res.reward);

                // Send feedback for learning
                if (obs.patient && taskId === "task1_esi_assignment" && selectedESI) {
                    triageApi.submitFeedback({
                        case_id: obs.patient.case_id,
                        task_id: taskId,
                        symptoms: obs.patient.symptoms,
                        human_esi: selectedESI,
                        reward: res.reward,
                    }).catch(() => { });
                }
            }
        } catch (e: unknown) { setError(String(e)); }
        finally { setLoading(false); }
    };

    // ── Doctor question (Task 3 human) ───────────────────────────────────────
    const handleAskQuestion = async (topic: QuestionTopic): Promise<string> => {
        setHumanActions((prev) => [...prev, `Asked doctor about: ${topic.replace(/_/g, " ")}`]);
        const res = await triageApi.step({ action_type: "ask_question", question_topic: topic, session_id: SESSION });
        setObs(res.observation);
        return res.observation.message;
    };

    // ── AI agent ─────────────────────────────────────────────────────────────
    const handleRunAgent = async () => {
        resetEpisodeState();
        setObs(null);
        setAgentRunning(true);
        try {
            await runAIAgent(
                taskId,
                (newObs) => setObs(newObs),
                (initial, final) => {
                    setInitialQueueForAnim(initial);
                    setSortedQueue(final);
                    setIsQueueAnimating(true);
                    // Stop animation flag after animation completes (~3s)
                    setTimeout(() => setIsQueueAnimating(false), 4000);
                },
                addChatMsg,
                async (finalScore, finalObs) => {
                    setScore(finalScore);
                    setEpisodeDone(true);
                    setObs(finalObs);
                    await fetchExplanation(SESSION + "-ai", finalScore);
                },
            );
        } catch (e: unknown) { setError(String(e)); }
        finally { setAgentRunning(false); }
    };

    const currentTask = TASKS.find((t) => t.id === taskId)!;
    const showActive = obs !== null || agentRunning || episodeDone;

    // ─────────────────────────────────────────────────────────────────────────
    return (
        <div className="min-h-screen flex flex-col">

            {/* ── top bar ──────────────────────────────────────────────────── */}
            <header className="sticky top-0 z-40 border-b border-slate-900/6 bg-white/90 backdrop-blur-xl">
                <div className="mx-auto max-w-[1400px] flex items-center gap-4 px-6 h-14">
                    <Logo />
                    <Link href="/" className="hidden md:flex items-center gap-2 text-sm font-medium mr-2 text-slate-400 hover:text-slate-600 transition-colors">
                        <ChevronRightIcon className="w-3 h-3" />
                        <ShieldPlusIcon className="w-3.5 h-3.5" />
                        ClinicalTriage
                        <ChevronRightIcon className="w-3 h-3 text-slate-300" />
                        <span className="text-slate-400">Dashboard</span>
                    </Link>

                    {/* task tabs */}
                    <div className="flex items-center gap-1 flex-1">
                        {TASKS.map((t) => (
                            <button
                                key={t.id}
                                onClick={() => { setTaskId(t.id); setObs(null); resetEpisodeState(); }}
                                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-all
                  ${taskId === t.id ? "bg-slate-900/6 text-slate-900 border border-slate-900/10" : "text-slate-400 hover:text-slate-600 hover:bg-slate-900/4"}`}
                            >
                                <span className="w-1.5 h-1.5 rounded-full" style={{ background: t.color }} />
                                {t.label}
                                <span className="text-[10px] px-1.5 py-0.5 rounded-full" style={{ background: t.color + "22", color: t.color }}>
                                    {t.badge}
                                </span>
                            </button>
                        ))}
                    </div>

                    {/* learning stats */}
                    {stats.total > 0 && (
                        <div className="hidden md:flex items-center gap-1.5 text-xs text-slate-500 border border-slate-900/8 rounded-full px-3 py-1.5">
                            <UsersIcon className="w-3 h-3" />
                            {stats.total} decisions
                            {stats.avg_reward !== null && <span className="text-[#1f9254]">· {(stats.avg_reward * 100).toFixed(0)}% avg</span>}
                        </div>
                    )}

                    {/* mode toggle */}
                    <div className="flex items-center gap-1 bg-slate-900/4 rounded-xl p-1 border border-slate-900/6">
                        {(["human", "ai"] as const).map((m) => (
                            <button
                                key={m}
                                onClick={() => setMode(m)}
                                className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-all
                  ${mode === m ? "bg-[#0369a1] text-white shadow-md" : "text-slate-400 hover:text-slate-600"}`}
                            >
                                {m === "human" ? <UserIcon className="w-3.5 h-3.5" /> : <BotIcon className="w-3.5 h-3.5" />}
                                {m === "human" ? "Human Mode" : "AI Agent"}
                            </button>
                        ))}
                    </div>
                </div>
            </header>

            {/* ── main ─────────────────────────────────────────────────────── */}
            <div className="flex-1 mx-auto max-w-[1400px] w-full px-6 py-6">

                {/* ─ start screen ─ */}
                {!showActive && (
                    <div className="flex flex-col items-center justify-center min-h-[60vh] gap-6 text-center">
                        <div className="p-5 rounded-3xl" style={{ background: currentTask.color + "14", color: currentTask.color }}>
                            <ShieldPlusIcon className="w-10 h-10" />
                        </div>
                        <div>
                            <h2 className="text-2xl font-bold mb-2 text-slate-900">{currentTask.label}</h2>
                            <p className="text-slate-500 text-sm max-w-md">
                                {taskId === "task1_esi_assignment" && "Evaluate a patient presentation and assign the correct ESI level (1–5)."}
                                {taskId === "task2_queue_priority" && "Five patients arrive at once. Drag to rank them from most to least urgent."}
                                {taskId === "task3_ambiguous_triage" && "Hidden medical history. Ask the doctor up to 3 questions, then assign ESI."}
                            </p>
                        </div>
                        {stats.total > 0 && mode === "ai" && (
                            <div className="flex items-center gap-2 text-xs px-4 py-2 rounded-full border border-[#bf5af2]/25 bg-[#bf5af2]/8 text-[#bf5af2]">
                                <BrainCircuitIcon className="w-3.5 h-3.5" />
                                AI learned from {stats.total} human decision{stats.total !== 1 ? "s" : ""}
                            </div>
                        )}
                        {error && (
                            <div className="flex items-center gap-2 text-[#ff2d55] bg-[#ff2d55]/8 border border-[#ff2d55]/20 rounded-xl px-4 py-3 text-sm max-w-md">
                                <AlertCircleIcon className="w-4 h-4 flex-shrink-0" />{error}
                            </div>
                        )}
                        <button
                            onClick={mode === "ai" ? handleRunAgent : handleReset}
                            disabled={loading || agentRunning}
                            className="btn-glow flex items-center gap-2 px-8 py-3.5 rounded-2xl font-semibold text-base"
                            style={{ background: currentTask.color, boxShadow: `0 0 32px ${currentTask.color}44` }}
                        >
                            {mode === "ai" ? <BotIcon className="w-5 h-5" /> : <PlayIcon className="w-5 h-5" />}
                            {mode === "ai" ? "Run AI Agent" : "Start Triage"}
                        </button>
                    </div>
                )}

                {/* ─ active episode ─ */}
                {showActive && (
                    <div className="grid grid-cols-1 xl:grid-cols-[1fr_400px] gap-5">

                        {/* LEFT ─── */}
                        <div className="flex flex-col gap-5">

                            {/* episode header */}
                            <div className="flex items-center justify-between">
                                <div className="flex items-center gap-3">
                                    <span className="text-sm text-slate-500">{mode === "ai" ? "AI Agent" : "Your turn"}</span>
                                    <span className="text-xs px-2.5 py-1 rounded-full font-medium" style={{ background: currentTask.color + "1a", color: currentTask.color }}>
                                        {currentTask.badge}
                                    </span>
                                    {agentRunning && (
                                        <span className="flex gap-1 items-center text-xs text-[#bf5af2]">
                                            <span className="relative flex h-2 w-2">
                                                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-[#bf5af2] opacity-75" />
                                                <span className="relative inline-flex rounded-full h-2 w-2 bg-[#bf5af2]" />
                                            </span>
                                            Running…
                                        </span>
                                    )}
                                </div>
                                <button
                                    onClick={mode === "ai" ? handleRunAgent : handleReset}
                                    disabled={loading || agentRunning}
                                    className="flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs border border-slate-900/8 text-slate-500 hover:text-slate-900 hover:border-slate-900/20 transition-all disabled:opacity-40"
                                >
                                    <RefreshCwIcon className={`w-3.5 h-3.5 ${loading || agentRunning ? "animate-spin" : ""}`} />
                                    {mode === "ai" ? "Run Again" : "Restart"}
                                </button>
                            </div>

                            {/* patient card */}
                            {obs?.patient && <PatientCard patient={obs.patient} />}

                            {/* Task 2: animated queue (AI) or draggable queue (human) */}
                            {(obs?.queue || initialQueueForAnim.length > 0) && taskId === "task2_queue_priority" && (
                                <div className="glass p-5">
                                    <h3 className="text-sm font-semibold mb-4 text-slate-600">
                                        {mode === "ai" ? (isQueueAnimating ? "🤖 Agent sorting patients…" : "Queue (sorted by agent)") : "Patient Queue — most urgent first"}
                                    </h3>
                                    {mode === "ai" ? (
                                        <AnimatedQueue
                                            patients={initialQueueForAnim.length ? initialQueueForAnim : (obs?.queue ?? [])}
                                            finalOrder={sortedQueue}
                                            isAnimating={isQueueAnimating}
                                        />
                                    ) : (
                                        <QueueReorder
                                            patients={orderedQueue.length ? orderedQueue : (obs?.queue ?? [])}
                                            onChange={setOrderedQueue}
                                            disabled={episodeDone}
                                        />
                                    )}
                                </div>
                            )}

                            {/* Task 3 agent chat */}
                            {taskId === "task3_ambiguous_triage" && mode === "ai" && (
                                <AgentChat messages={agentChatMessages} isRunning={agentRunning} />
                            )}

                            {/* clarification message — hide for task2 (backend sends raw Kendall Tau text with case IDs) */}
                            {obs?.message && (obs.step ?? 0) > 0 && mode === "human" && taskId !== "task2_queue_priority" && (
                                <div className="glass p-4 text-sm text-slate-600 border-l-2 border-[#0369a1]/50 rounded-l-none animate-fade-up">
                                    {obs.message}
                                </div>
                            )}

                            {/* ─ result card ── */}
                            {episodeDone && score !== null && (
                                <div
                                    className="glass p-6 animate-fade-up border"
                                    style={{ borderColor: score >= 0.8 ? "#1f9254" : score >= 0.4 ? "#b45309" : "#ff2d55" }}
                                >
                                    <div className="flex items-center gap-4 mb-2">
                                        <TrophyIcon className="w-8 h-8" style={{ color: score >= 0.8 ? "#1f9254" : score >= 0.4 ? "#b45309" : "#ff2d55" }} />
                                        <div>
                                            <div className="text-2xl font-bold" style={{ color: score >= 0.8 ? "#1f9254" : score >= 0.4 ? "#b45309" : "#ff2d55" }}>
                                                Score: {(score * 100).toFixed(0)}%
                                            </div>
                                            <div className="text-xs text-slate-500 mt-0.5">
                                                {score === 1 ? "Perfect — exact match!" : score >= 0.4 ? "Partial credit" : "Incorrect — study the explanation below"}
                                            </div>
                                        </div>
                                    </div>

                                    {/* human action timeline */}
                                    {mode === "human" && humanActions.length > 0 && (
                                        <div className="mt-3 pt-3 border-t border-slate-900/8">
                                            <div className="text-xs text-slate-400 mb-2">Your actions:</div>
                                            {humanActions.map((a, i) => (
                                                <div key={i} className="text-xs text-slate-600 flex items-start gap-2 mb-1">
                                                    <span className="text-[#0369a1] mt-0.5">→</span>{a}
                                                </div>
                                            ))}
                                        </div>
                                    )}

                                    <button
                                        onClick={mode === "ai" ? handleRunAgent : handleReset}
                                        disabled={agentRunning}
                                        className="btn-glow mt-4 flex items-center gap-2 px-5 py-2.5 rounded-xl text-sm font-medium text-white bg-[#0369a1] disabled:opacity-40"
                                    >
                                        <RefreshCwIcon className="w-4 h-4" />
                                        {mode === "ai" ? "Run Agent Again" : "Try Again"}
                                    </button>
                                </div>
                            )}

                            {/* ─ Explanation panel ── */}
                            {episodeDone && explanation && (
                                <ExplanationPanel
                                    data={explanation}
                                    humanESI={humanESI}
                                    humanQueueOrder={humanQueueOrder}
                                    mode={mode}
                                />
                            )}

                            {/* ESI selector (Task 1 & Task 3 human) */}
                            {mode === "human" && !episodeDone && obs &&
                                (taskId === "task1_esi_assignment" ||
                                    (taskId === "task3_ambiguous_triage" &&
                                        (obs.awaiting_final_esi || (obs.clarification_budget ?? 3) === 0 || selectedESI !== null))) && (
                                    <div className="glass p-5">
                                        <h3 className="text-sm font-semibold mb-4 text-slate-600">Assign ESI Level</h3>
                                        {selectedESI && <div className="mb-4"><ESIBadge level={selectedESI} /></div>}
                                        <ESISelector selected={selectedESI} onSelect={setSelectedESI} disabled={episodeDone} />
                                        <button
                                            onClick={handleHumanSubmit}
                                            disabled={!selectedESI || loading}
                                            className="btn-glow mt-4 w-full py-3 rounded-xl font-semibold text-sm text-white bg-[#0369a1] disabled:opacity-40 disabled:cursor-not-allowed"
                                        >
                                            {loading ? "Submitting…" : "Submit Triage Decision →"}
                                        </button>
                                    </div>
                                )}

                            {/* Task 2 submit */}
                            {mode === "human" && !episodeDone && obs && taskId === "task2_queue_priority" && (
                                <button
                                    onClick={handleHumanSubmit}
                                    disabled={loading}
                                    className="btn-glow w-full py-3.5 rounded-2xl font-semibold text-sm bg-[#b45309] text-white disabled:opacity-40"
                                >
                                    {loading ? "Submitting…" : "Submit Queue Order →"}
                                </button>
                            )}

                            {/* Task 3 skip to ESI */}
                            {mode === "human" && !episodeDone && obs &&
                                taskId === "task3_ambiguous_triage" && !obs.awaiting_final_esi &&
                                (obs.clarification_budget ?? 3) > 0 && selectedESI === null && (
                                    <div className="glass p-4 flex items-center justify-between text-sm text-slate-400">
                                        <span>Ask the doctor questions first, then assign ESI when ready.</span>
                                        <button
                                            onClick={() => setObs((prev) => prev ? { ...prev, awaiting_final_esi: true } : prev)}
                                            className="text-[#0369a1] hover:underline text-xs ml-4 flex-shrink-0"
                                        >
                                            Skip to ESI →
                                        </button>
                                    </div>
                                )}
                        </div>

                        {/* RIGHT ─── */}
                        <div className="flex flex-col gap-5">

                            {/* Task 3 doctor chat (human mode) */}
                            {taskId === "task3_ambiguous_triage" && mode === "human" && obs && (
                                <DoctorChat budget={obs.clarification_budget ?? 3} onAskQuestion={handleAskQuestion} disabled={episodeDone} />
                            )}

                            {/* AI: run again */}
                            {mode === "ai" && !agentRunning && episodeDone && (
                                <button
                                    onClick={handleRunAgent}
                                    className="btn-glow flex items-center justify-center gap-2 py-3.5 rounded-2xl font-semibold text-sm text-white bg-[#bf5af2]"
                                >
                                    <BotIcon className="w-4 h-4" />
                                    Run Agent Again
                                </button>
                            )}
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
}
