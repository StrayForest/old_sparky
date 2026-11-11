import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CspImage, type CspImageProps } from "@/components/media/csp-image";
import { deadlockHeroIconPath, deadlockHeroPlaceholderPath } from "@/lib/deadlock";

type DeadlockHeroImageProps = Omit<CspImageProps, "src"> & {
  heroName: string;
  imageUrl?: string | null;
};

export function DeadlockHeroImage({ heroName, imageUrl, onError, ...props }: DeadlockHeroImageProps) {
  const candidates = useMemo(() => [
    imageUrl,
    `/assets/heroes/${encodeURIComponent(heroName.replaceAll(" ", "_"))}.png`,
    deadlockHeroIconPath(heroName),
    deadlockHeroPlaceholderPath,
  ].filter((candidate, index, all): candidate is string => Boolean(candidate) && all.indexOf(candidate) === index), [heroName, imageUrl]);
  const sourceIdentity = `${heroName}\0${imageUrl ?? ""}`;
  const [selection, setSelection] = useState({ sourceIdentity, index: 0 });
  const candidateIndex = selection.sourceIdentity === sourceIdentity ? selection.index : 0;
  const imageRef = useRef<HTMLImageElement | null>(null);
  const failedSourcesRef = useRef({ sourceIdentity, sources: new Set<string>() });
  const src = candidates[Math.min(candidateIndex, candidates.length - 1)] ?? deadlockHeroPlaceholderPath;

  const advanceAfterFailure = useCallback((image: HTMLImageElement) => {
    const failedSource = image.getAttribute("src") ?? "";
    const failedIndex = candidates.indexOf(failedSource);
    if (failedIndex < 0 || failedIndex >= candidates.length - 1) {
      return;
    }
    const failedSources = failedSourcesRef.current.sourceIdentity === sourceIdentity
      ? failedSourcesRef.current.sources
      : new Set<string>();
    if (failedSources.has(failedSource)) return;
    failedSources.add(failedSource);
    failedSourcesRef.current = { sourceIdentity, sources: failedSources };
    setSelection((previous) => {
      const currentIndex = previous.sourceIdentity === sourceIdentity ? previous.index : 0;
      const nextIndex = Math.max(currentIndex, failedIndex + 1);
      return { sourceIdentity, index: nextIndex };
    });
  }, [candidates, sourceIdentity]);

  useEffect(() => {
    const image = imageRef.current;
    if (
      image?.complete
      && image.naturalWidth === 0
      && image.getAttribute("src") === src
      && image.currentSrc
      && window.performance.getEntriesByName(image.currentSrc).length > 0
      && candidateIndex === 0
    ) {
      advanceAfterFailure(image);
    }
  }, [advanceAfterFailure, candidateIndex, candidates.length, src]);

  return (
    <CspImage
      {...props}
      ref={imageRef}
      onError={(event) => {
        advanceAfterFailure(event.currentTarget);
        onError?.(event);
      }}
      src={src}
    />
  );
}
