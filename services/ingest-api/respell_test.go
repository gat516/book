package main

import "testing"

func TestRespellForSkipsSwapsOrRetranslates(t *testing.T) {
	ready := QueueMessage{NovelID: "n", ChapterIndex: 1, Retranslate: true}

	if _, needed := respellFor(ready, "Long Fei", "Long Fei"); needed {
		t.Fatal("confirming the spelling already in the text must queue nothing")
	}
	msg, needed := respellFor(ready, "Dragon Fei", "Long Fei")
	if !needed || len(msg.Respell) != 1 || msg.Respell[0] != (Respelling{From: "Dragon Fei", To: "Long Fei"}) {
		t.Fatalf("a changed spelling must be swapped, got %+v", msg)
	}
	if msg, needed := respellFor(ready, "", "Long Fei"); !needed || msg.Respell != nil {
		t.Fatalf("an unknown primed spelling must retranslate, got %+v", msg)
	}
	waiting := QueueMessage{NovelID: "n", ChapterIndex: 2}
	if msg, needed := respellFor(waiting, "Long Fei", "Long Fei"); !needed || msg.Respell != nil {
		t.Fatalf("an untranslated chapter waiting on review must still be queued, got %+v", msg)
	}
}
