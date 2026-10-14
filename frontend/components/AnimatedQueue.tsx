"use client";

import { useEffect, useRef, useState } from "react";
import { motion } from "framer-motion";
import { PatientSummary } from "@/lib/types";
import { getPatientName } from "@/lib/names";

interface Props {
    patients: PatientSummary[];       // initial order (shown before & during animation)
    finalOrder: PatientSummary[];     // agent's sorted order (animate toward this)
    isAnimating: boolean;
    disabled?: boolean;
    onComplete?: () => void;
    
}

export function AnimatedQueue({ patients, finalOrder, isAnimating, onComplete }: Props) {
    // displayOrder is the single source of truth for what's shown
    const [displayOrder, setDisplayOrder] = useState<PatientSummary[]>(patients);
    const [activeIdx, setActiveIdx] = useState<number | null>(null);

    // Ref to track ongoing animation — prevents new effect runs from interfering
    const animRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const isRunningRef = useRef(false);

    // Only reset to patients prop when NOT animating (e.g. fresh episode)
    useEffect(() => {
        if (!isAnimating && !isRunningRef.current) {
            setDisplayOrder(patients);
        }
    }, [patients, isAnimating]);

    // Animation effect — runs once when isAnimating flips to true
    useEffect(() => {
        if (!isAnimating || finalOrder.length === 0) return;
        if (isRunningRef.current) return; // already running, ignore re-trigger

        isRunningRef.current = true;

        // Capture the starting order at this moment (don't rely on prop later)
        const startOrder = patients.map((p) => ({ ...p }));

        // Derive bubble-sort swap steps from startOrder → finalOrder
        const target = finalOrder.map((p) => p.case_id);
        const working = startOrder.map((p) => p.case_id);
        const steps: [number, number][] = [];

        for (let i = 0; i < target.length; i++) {
            const curIdx = working.indexOf(target[i]);
            if (curIdx !== i) {
                for (let j = curIdx; j > i; j--) {
                    steps.push([j - 1, j]);
                    [working[j - 1], working[j]] = [working[j], working[j - 1]];
                }
            }
        }

        if (steps.length === 0) {
            setDisplayOrder([...finalOrder]);
            isRunningRef.current = false;
            onComplete?.();
            return;
        }

        let stepIdx = 0;
        const current = [...startOrder];

        const runStep = () => {
            if (stepIdx >= steps.length) {
                setDisplayOrder([...finalOrder]);
                setActiveIdx(null);
                isRunningRef.current = false;
                onComplete?.();
                return;
            }
            const [a, b] = steps[stepIdx];
            setActiveIdx(a);
            [current[a], current[b]] = [current[b], current[a]];
            // Replace the whole array reference so React detects the change
            setDisplayOrder(current.map((p) => ({ ...p })));
            stepIdx++;
            animRef.current = setTimeout(runStep, 500);
        };

        animRef.current = setTimeout(runStep, 400);

        return () => {
            if (animRef.current) clearTimeout(animRef.current);
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [isAnimating]); // ← intentionally only depends on isAnimating, not patients/finalOrder

    return (
        <div className="space-y-2">
            {displayOrder.map((p, idx) => (
                <motion.div
                    key={p.case_id}
                    layout
                    transition={{ type: "spring", stiffness: 420, damping: 34, mass: 0.9 }}
                    className={`flex items-center gap-3 rounded-xl p-3 border
                        ${activeIdx === idx
                            ? "border-[#0369a1]/50 bg-[#0369a1]/8 shadow-[0_0_16px_#0369a11a]"
                            : "border-slate-900/8 bg-slate-900/[0.015]"
                        }`}
                >
                    {/* rank badge */}
                    <div className="w-6 h-6 rounded-lg bg-slate-900/6 flex items-center justify-center text-xs font-bold text-slate-400 flex-shrink-0">
                        {idx + 1}
                    </div>

                    {/* status dot */}
                    {activeIdx === idx && isRunningRef.current ? (
                        <span className="w-2 h-2 rounded-full bg-[#0369a1] animate-pulse flex-shrink-0" />
                    ) : (
                        <span className="w-2 h-2 rounded-full flex-shrink-0" style={{ background: "#b45309" }} />
                    )}

                    {/* patient info */}
                    <div className="flex-1 min-w-0">
                        <div className="text-sm font-semibold text-slate-900 truncate">
                            {getPatientName(p.case_id, p.gender)}
                        </div>
                        <div className="flex items-center gap-2 mt-0.5">
                            <span className="text-xs text-slate-500">{p.age}y · {p.gender}</span>
                            <span className="text-[10px] font-mono text-slate-300">{p.case_id}</span>
                        </div>
                        <div className="text-xs text-slate-400 truncate mt-0.5">
                            {p.symptoms.slice(0, 2).join(", ")}{p.symptoms.length > 2 ? "…" : ""}
                        </div>
                    </div>

                    {/* moving indicator */}
                    {activeIdx === idx && isRunningRef.current && (
                        <span className="text-xs text-[#0369a1] font-medium animate-pulse flex-shrink-0">↕ moving</span>
                    )}
                </motion.div>
            ))}
        </div>
    );
}
