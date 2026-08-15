from scripts import production_v2_integrated_qualification as qualification


def test_character_layout_scales_deepseek_and_anchors_every_matte_behind_desk() -> None:
    layouts = {
        participant_id: qualification._character_layout(participant_id)
        for participant_id in qualification.PARTICIPANTS
    }

    assert layouts["deepseek"]["canvas_size"] == 414
    assert layouts["deepseek"]["canvas_size"] > layouts["chatgpt"]["canvas_size"]
    assert {
        layout["target_alpha_bottom"] for layout in layouts.values()
    } == {qualification.DESK_TOP + qualification.DESK_OCCLUSION_OVERLAP}

    for participant_id, layout in layouts.items():
        geometry = qualification.MATTE_GEOMETRY[participant_id]
        rendered_alpha_bottom = layout["top"] + round(
            geometry["alpha_bottom"] * layout["canvas_size"] / geometry["canvas"]
        )
        assert rendered_alpha_bottom == layout["target_alpha_bottom"]


def test_presentation_blend_uses_segment_local_frame_clock() -> None:
    entering = qualification._presentation_blend(2, 2.0)
    leaving = qualification._presentation_blend(3, 2.0)

    assert "N" in entering
    assert "T" not in entering
    assert "cos" in entering
    assert "N" in leaving
    assert "T" not in leaving
    assert qualification._presentation_blend(0, 2.0) == "A"
