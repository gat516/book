package main

import (
	"strings"
	"testing"
)

// The Go half of a cross-language invariant: these are the exact cases
// services/pipeline/tests/test_resolve.py asserts against _source_term_problem. A term one
// side locks and the other refuses means RESOLVE and the human bootstrap endpoint disagree
// about what is lockable, which is the divergence porting the guard exists to prevent.
func TestSourceTermProblemMatchesResolvePy(t *testing.T) {
	lockable := []string{
		"九神殿",
		"青云宗",
		"Azure Cloud Sect",
		"St. Mary", // an ASCII period belongs inside a name, not to sentence punctuation
		"  九神殿  ",
	}
	for _, term := range lockable {
		if problem := sourceTermProblem(term); problem != "" {
			t.Errorf("sourceTermProblem(%q) = %q, want lockable", term, problem)
		}
	}

	refused := map[string]string{
		// One character is a morpheme, not a name: locking 神 rewrites 精神 into "精God".
		"神": "too short",
		"A": "too short",
		"神职细分为四个层次：主宰，一级神职。": "prose",
		"九神殿的大门打开了。":         "prose",
		"two\nlines":         "prose",
	}
	for term, want := range refused {
		problem := sourceTermProblem(term)
		if problem == "" {
			t.Errorf("sourceTermProblem(%q) = lockable, want refused (%s)", term, want)
			continue
		}
		if !strings.Contains(problem, want) {
			t.Errorf("sourceTermProblem(%q) = %q, want it to mention %q", term, problem, want)
		}
	}

	// Length limits count runes, not bytes — otherwise a CJK name would hit the cap at a
	// third of the characters Python counts.
	long := ""
	for i := 0; i < 90; i++ {
		long += "神"
	}
	if problem := sourceTermProblem(long); problem == "" {
		t.Errorf("sourceTermProblem(90 CJK runes) = lockable, want refused")
	}
	justUnder := ""
	for i := 0; i < maxSourceTermChars; i++ {
		justUnder += "神"
	}
	if problem := sourceTermProblem(justUnder); problem != "" {
		t.Errorf("sourceTermProblem(%d CJK runes) = %q, want lockable", maxSourceTermChars, problem)
	}
}
