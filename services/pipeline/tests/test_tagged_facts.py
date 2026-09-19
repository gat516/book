from pipeline.tagged_facts import TaggedFact, mark_names, parse_tagged


def test_tagged_lines_parse_and_a_malformed_line_drops_only_itself():
    facts = parse_tagged("""thinking...
## Facts
- event | A left the city.
relationship: sworn brother | A is B's sworn brother.
relationship | a bond with no kind
weather | it rained
2. status | B was injured.
""")
    assert facts == [
        TaggedFact("event", None, "A left the city."),
        TaggedFact("relationship", "sworn brother", "A is B's sworn brother."),
        TaggedFact("status", None, "B was injured."),
    ]


def test_names_become_markers_longest_first_in_order_of_appearance():
    spellings = {"c1": "Long Fei", "c2": "Lord Long Fei Senior", "c3": "Yan"}
    text, order = mark_names("Yan is Long Fei's lover; Lord Long Fei Senior watched. Yanny left.", spellings)
    assert text == "⟦c3⟧ is ⟦c1⟧'s lover; ⟦c2⟧ watched. Yanny left."
    assert order == ["c3", "c1", "c2"]


def test_a_fact_naming_nobody_is_left_as_it_is():
    assert mark_names("The gates opened.", {"c1": "Ares"}) == ("The gates opened.", [])


def test_place_facts_and_the_to_form_of_relationships_parse():
    from pipeline.tagged_facts import says_none
    facts = parse_tagged("## Facts\n\nplace | The valley is sealed.\nrelationship: subordinate | A is subordinate to B.\n")
    assert facts == [TaggedFact("place", None, "The valley is sealed."),
                     TaggedFact("relationship", "subordinate", "A is subordinate to B.")]
    assert says_none("## Facts\n\nNone\n") and not says_none("## Facts\nevent | x")
