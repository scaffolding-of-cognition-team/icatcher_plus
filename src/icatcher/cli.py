import logging
import torch
import numpy as np
import cv2
from PIL import Image
from pathlib import Path
import pooch
from icatcher import (
    version,
    classes,
    reverse_classes,
    options,
    draw,
    video,
    models,
    parsers,
    ui_packaging,
)
from icatcher.face_detector import (
    extract_bboxes,
    process_frames,
    parallelize_face_detection,
    detect_face_opencv_dnn,
)
from batch_face import RetinaFace
from icatcher.icatcher_app.api import run_icatcher_app


def select_face(bboxes, frame, fc_model, fc_data_transforms, hor, ver, device):
    """
    selects a correct face from candidates bbox in frame
    :param bboxes: the bounding boxes of candidates
    :param frame: the frame
    :param fc_model: a classifier model, if passed it is used to decide.
    :param fc_data_transforms: the transformations to apply to the images before fc_model sees them
    :param hor: the last known horizontal correct face location
    :param ver: the last known vertical correct face location
    :return: the cropped face and its bbox data
    """
    if fc_model:
        centers = []
        faces = []
        for box in bboxes:
            crop_img = frame[box[1] : box[1] + box[3], box[0] : box[0] + box[2]]
            face_box = np.array([box[1], box[1] + box[3], box[0], box[0] + box[2]])
            img_shape = np.array(frame.shape)
            ratio = np.array(
                [
                    face_box[0] / img_shape[0],
                    face_box[1] / img_shape[0],
                    face_box[2] / img_shape[1],
                    face_box[3] / img_shape[1],
                ]
            )
            face_ver = (ratio[0] + ratio[1]) / 2
            face_hor = (ratio[2] + ratio[3]) / 2

            centers.append([face_hor, face_ver])
            img = crop_img
            img = fc_data_transforms["val"](img)
            faces.append(img)
        centers = np.stack(centers)
        faces = torch.stack(faces).to(device)
        output = fc_model(faces)
        _, preds = torch.max(output, 1)
        preds = preds.cpu().numpy()
        idxs = np.where(preds == 0)[0]
        if idxs.size == 0:
            bbox = None
        else:
            centers = centers[idxs]
            dis = np.sqrt((centers[:, 0] - hor) ** 2 + (centers[:, 1] - ver) ** 2)
            i = np.argmin(dis)
            # crop_img = faces[idxs[i]]
            bbox = bboxes[idxs[i]]
            # hor, ver = centers[i]
    else:  # select face based on a mix of the lowest face and the width ratio of the face
        bbox = None
        prev_score = 0
        for box in bboxes:
            top_left_x, top_left_y, width, height = box
            # make sure not dividing by zero
            if width == 0 or height == 0:
                continue
            else:
                # find min ratio of width and height which will weight box score
                min_ratio = min(width, height) / max(width, height)
                box_bottom = top_left_y + height
                box_score = min_ratio * box_bottom

                # check if score outweighs previous bounding boxes
                if box_score > prev_score:
                    prev_score = box_score
                    bbox = box
    return bbox


def fix_illegal_transitions(
    loc, answers, confidences, illegal_transitions, corrected_transitions
):
    """
    fixes illegal transitions happening in answers at [loc-max_trans_len+1, loc] inclusive
    """
    for i, transition in enumerate(illegal_transitions):
        len_trans = len(transition)
        buffer = answers[loc - len_trans + 1 : loc + 1]
        if buffer == transition:
            buffer_update = corrected_transitions[i]
            answers[loc - len_trans + 1 : loc + 1] = buffer_update
            buffer_splits = np.where(np.array(buffer_update) != np.array(buffer))
            for spot in buffer_splits[0].tolist():
                confidences[loc - len_trans + 1 + spot] = -1
    return answers, confidences


def extract_crop(frame, bbox, opt):
    """
    extracts a crop from a frame using bbox, and transforms it
    :param frame: the frame
    :param bbox: opencv bbox 4x1
    :param opt: command line options
    :return: the crop and the 5x1 box features
    """
    if bbox is None:
        return None, None

    # make sure no negatives being fed into extract crop
    bbox = [0 if x < 0 else x for x in bbox]

    img_shape = np.array(frame.shape)
    face_box = np.array([bbox[1], bbox[1] + bbox[3], bbox[0], bbox[0] + bbox[2]])
    crop = frame[bbox[1] : bbox[1] + bbox[3], bbox[0] : bbox[0] + bbox[2]]

    test_transforms = models.DataTransforms(
        opt.image_size, opt.per_channel_mean, opt.per_channel_std
    ).transformations["test"]
    crop = test_transforms(Image.fromarray(crop))
    crop = crop.permute(1, 2, 0).unsqueeze(0).numpy()
    ratio = np.array(
        [
            face_box[0] / img_shape[0],
            face_box[1] / img_shape[0],
            face_box[2] / img_shape[1],
            face_box[3] / img_shape[1],
        ]
    )
    face_size = (ratio[1] - ratio[0]) * (ratio[3] - ratio[2])
    face_ver = (ratio[0] + ratio[1]) / 2
    face_hor = (ratio[2] + ratio[3]) / 2
    face_height = ratio[1] - ratio[0]
    face_width = ratio[3] - ratio[2]
    my_box = np.array([face_size, face_ver, face_hor, face_height, face_width])
    return crop, my_box


def load_models(opt, download_only=False):
    """
    loads all relevant neural network models to perform predictions
    models will be automatically downloaded if not found in the cache,
    user may overide downloaded location with the env variable ICATCHER_DATA_DIR
    defaults:
    :Mac: "~/Library/Caches/<AppName>"
    :Unix: "~/.cache/<AppName>" or the value of the "XDG_CACHE_HOME"
    environment variable, if defined.
    :Windows: "C:\\Users\\<user>\\AppData\\Local\\<AppAuthor>\\<AppName>\\Cache"
    :param opt: command line options
    :param download_only: if true will only download the models without loading them
    :return all nn models
    """
    GOODBOY = pooch.create(
        path=pooch.os_cache("icatcher_plus"),
        base_url="https://osf.io/h7svp/download",
        version=version,
        version_dev="main",
        env="ICATCHER_DATA_DIR",
        registry={"zip_content.txt": None, "icatcher+_models.zip": None},
        urls={
            "zip_content.txt": "https://osf.io/v4w53/download",
            "icatcher+_models.zip": "https://osf.io/h7svp/download",
        },
    )
    # zip_content_file = GOODBOY.fetch("zip_content.txt")
    # with open(zip_content_file, "r") as f:
    # zip_content = [x.strip() for x in f]
    file_paths = GOODBOY.fetch(
        "icatcher+_models.zip", processor=pooch.Unzip(), progressbar=True
    )

    if not download_only:
        file_names = [Path(x).name for x in file_paths]
        if opt.fd_model == "retinaface":
            face_detector_model_file = file_paths[
                file_names.index("Resnet50_Final.pth")
            ]
            face_detector_model = RetinaFace(
                gpu_id=opt.gpu_id,
                model_path=face_detector_model_file,
                network="resnet50",
            )
        elif opt.fd_model == "opencv_dnn":
            face_detector_model_file = file_paths[
                file_names.index("face_model.caffemodel")
            ]
            config_file = file_paths[file_names.index("config.prototxt")]
            face_detector_model = cv2.dnn.readNetFromCaffe(
                str(config_file), str(face_detector_model_file)
            )
        else:
            raise NotImplementedError
        try:
            path_to_gaze_model = Path(file_paths[file_names.index(opt.model)])
        except:
            print("Model {} not found in the downloaded files, so assuming it is a full path".format(opt.model))
            path_to_gaze_model = Path(opt.model)

        is_regnet = "regnet" in str(path_to_gaze_model.stem)
        path_to_fc_model = file_paths[file_names.index(opt.fc_model)]
        gaze_model = models.GazeCodingModel(opt, is_regnet=is_regnet).to(opt.device)
        if opt.device == "cpu":
            state_dict = torch.load(
                str(path_to_gaze_model), map_location=torch.device(opt.device)
            )
        else:
            state_dict = torch.load(str(path_to_gaze_model))
        try:
            gaze_model.load_state_dict(state_dict)
        except (
            RuntimeError
        ) as e:  # hack to deal with models trained on distributed setup
            from collections import OrderedDict

            new_state_dict = OrderedDict()
            for k, v in state_dict.items():
                name = k[7:]  # remove `module.`
                new_state_dict[name] = v
            # load params
            gaze_model.load_state_dict(new_state_dict)
        gaze_model.eval()

        if opt.use_fc_model:
            face_classifier_model, fc_input_size = models.init_face_classifier(
                opt.device, num_classes=2, resume_from=path_to_fc_model
            )
            face_classifier_model.eval()
            face_classifier_model.to(opt.device)
            face_classifier_data_transforms = models.get_fc_data_transforms(
                fc_input_size
            )
        else:
            face_classifier_model = None
            face_classifier_data_transforms = None
        return (
            gaze_model,
            face_detector_model,
            face_classifier_model,
            face_classifier_data_transforms,
        )


def create_output_streams(video_path, framerate, resolution, opt):
    """
    creates output streams
    :param video_path: path to video
    :param framerate: video framerate
    :param resolution: video resolution
    :param opt: options
    :return: video_output_file, prediction_output_file, ui_output_components, skip = prediction file already exists
    """
    video_output_file = None
    prediction_output_file = None
    ui_output_components = None
    skip = False

    fourcc = cv2.VideoWriter_fourcc(
        *"MP4V"
    )  # may need to be adjusted per available codecs & OS
    if opt.output_video_path:
        my_video_path = Path(opt.output_video_path, video_path.stem + "_output.mp4")
        video_output_file = cv2.VideoWriter(
            str(my_video_path), fourcc, framerate, resolution, True
        )
    if opt.output_annotation:
        if opt.output_format == "compressed":
            prediction_output_file = Path(opt.output_annotation, video_path.stem)
            npz_extension = Path(str(prediction_output_file) + ".npz")
            if npz_extension.exists():
                if opt.overwrite:
                    npz_extension.unlink()
                else:
                    raise FileExistsError(
                        "Annotation output file already exists. Use --overwrite flag to overwrite."
                    )
        elif opt.output_format == "raw_output":
            prediction_output_file = Path(
                opt.output_annotation, video_path.stem + opt.output_file_suffix
            )
            if prediction_output_file.exists():
                if opt.overwrite:
                    prediction_output_file.unlink()
                else:
                    raise FileExistsError(
                        "Annotation output file already exists. Use --overwrite flag to overwrite."
                    )
        elif opt.output_format == "ui":
            ui_output_components = ui_packaging.prepare_ui_output_components(
                opt.output_annotation,
                video_path,
                opt.overwrite,
            )
        else:
            raise NotImplementedError(
                "output format {} not implemented".format(opt.output_annotation)
            )

    return video_output_file, prediction_output_file, ui_output_components, skip


def predict_from_video(opt):
    """
    perform prediction on a stream or video file(s) using a network.
    output can be of various kinds, see options for details.
    :param opt: command line arguments
    :return:
    """
    # initialize
    # loc determines where in the sliding window to take the prediction, fixed to be the middle frame
    loc = -((opt.sliding_window_size // 2) + 1)
    # cursor points to the frame we will write to output relative to current frame, it can change based on illegal transitions
    cursor = -((opt.sliding_window_size // 2) + 1)
    logging.debug(
        "using the following values for per-channel mean: {}".format(
            opt.per_channel_mean
        )
    )
    logging.debug(
        "using the following values for per-channel std: {}".format(opt.per_channel_std)
    )
    (
        gaze_model,
        face_detector_model,
        face_classifier_model,
        face_classifier_data_transforms,
    ) = load_models(opt)
    video_paths = video.get_video_paths(opt)
    if opt.illegal_transitions_path:
        (
            illegal_transitions,
            corrected_transitions,
        ) = parsers.parse_illegal_transitions_file(opt.illegal_transitions_path)
        max_illegal_transition_length = max(
            [len(transition) for transition in illegal_transitions]
        )
        cursor -= max_illegal_transition_length  # slide cursor back so all illegal transitions can be fixed on the fly
        if abs(cursor) > opt.sliding_window_size:
            raise ValueError(
                "illegal_transitions_path contains transitions longer than the sliding window size"
            )
    # check if cpu or gpu being used
    use_cpu = True if opt.gpu_id == -1 else False
    # loop over inputs
    for i in range(len(video_paths)):
        video_path = Path(str(video_paths[i]))
        logging.debug("predicting on : {}".format(video_path))
        (
            cap,
            framerate,
            resolution,
            h_start_at,
            h_end_at,
            w_start_at,
            w_end_at,
        ) = video.process_video(video_path, opt)
        (
            video_output_file,
            prediction_output_file,
            ui_output_components,
            skip,
        ) = create_output_streams(video_path, framerate, resolution, opt)
        if skip:
            continue
        # per video initialization
        answers = []  # list of answers for each frame
        confidences = []  # list of confidences for each frame
        image_sequence = (
            []
        )  # list of (crop, valid) for each frame in the sliding window
        box_sequence = []  # list of bounding boxes for each frame in the sliding window
        bbox_sequence = (
            []
        )  # list of bounding boxes for each frame in the sliding window
        frames = []  # list of frames for each frame in the sliding window
        from_tracker = (
            []
        )  # list of booleans indicating whether the bounding box was obtained from the tracker
        last_known_valid_bbox = None  # last known valid bounding box
        frame_count = 0  # frame counter
        hor, ver = 0.5, 1  # initial guess for face location
        cur_fps = video.FPS()  # for debugging purposes
        last_class_text = ""  # Initialize so that we see the first class assignment as an event to record

        # if going to use cpu parallelization, don't allow for live stream video
        if use_cpu and opt.fd_model == "retinaface" and opt.fd_parallel_processing:
            # send all frames in to be preprocessed and have faces detected prior to running gaze detection
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            vid_frames = range(
                0, total_frames, 1 + opt.fd_skip_frames
            )  # adding step if frames are skipped
            processed_frames = process_frames(
                cap, vid_frames, h_start_at, h_end_at, w_start_at, w_end_at
            )
            frame_height, frame_width = (
                processed_frames[0].shape[0],
                processed_frames[0].shape[1],
            )
            logging.info("performing face detection on buffered frames...")
            faces = parallelize_face_detection(
                processed_frames, face_detector_model, opt.fd_num_cpus, opt
            )
            del processed_frames

            # flatten the list and extract bounding boxes
            faces = [item for sublist in faces for item in sublist]
            master_bboxes = [
                extract_bboxes(face_group, frame_height, frame_width)
                for face_group in faces
            ]

            # if frames were skipped, need to repeat binding boxes for that many skips
            if opt.fd_skip_frames > 0:
                master_bboxes = np.repeat(master_bboxes, opt.fd_skip_frames + 1)

            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)  # reset frames to 0

        # loop over frames (refactor !)
        ret_val, frame = cap.read()
        frame_height, frame_width = frame.shape[0], frame.shape[1]
        while ret_val:
            frame = draw.mask_regions(
                frame, h_start_at, h_end_at, w_start_at, w_end_at
            )  # mask roi
            frames.append(frame)

            if (
                use_cpu and opt.fd_model == "retinaface" and opt.fd_parallel_processing
            ):  # if using cpu, just pull from master
                bboxes = master_bboxes[frame_count]
            elif opt.fd_model == "opencv_dnn":
                bboxes = detect_face_opencv_dnn(
                    face_detector_model, frame, opt.fd_confidence_threshold
                )
            else:  # uses retina face, if using gpu, find face as frame is processed... don't need batch inference
                faces = face_detector_model(frame)
                faces = [
                    face for face in faces if face[-1] >= opt.fd_confidence_threshold
                ]
                bboxes = extract_bboxes(faces, frame_height, frame_width)
            frame = cv2.cvtColor(
                frame, cv2.COLOR_BGR2RGB
            )  # network was trained on RGB images.
            if not bboxes and (last_known_valid_bbox is None or not opt.track_face):
                answers.append(
                    classes["noface"]
                )  # if face detector fails, treat as away and mark invalid
                confidences.append(-1)
                image = np.zeros((1, opt.image_size, opt.image_size, 3), np.float64)
                my_box = np.array([0, 0, 0, 0, 0])
                image_sequence.append((image, False))
                box_sequence.append(my_box)
                bbox_sequence.append(None)
                from_tracker.append(False)
            else:
                if bboxes:
                    from_tracker.append(False)
                else:
                    from_tracker.append(True)
                    bboxes = [last_known_valid_bbox]
                selected_bbox = select_face(
                    bboxes,
                    frame,
                    face_classifier_model,
                    face_classifier_data_transforms,
                    hor,
                    ver,
                    opt.device,
                )
                crop, my_box = extract_crop(frame, selected_bbox, opt)
                if selected_bbox is None:
                    # if selecting face fails, treat as away and mark invalid
                    answers.append(classes["nobabyface"])
                    confidences.append(-1)
                    image = np.zeros((1, opt.image_size, opt.image_size, 3), np.float64)
                    my_box = np.array([0, 0, 0, 0, 0])
                    image_sequence.append((image, False))
                    box_sequence.append(my_box)
                    bbox_sequence.append(None)
                else:
                    # if face detector succeeds, treat as "none" (will be overwritten later) and mark valid
                    if crop.size == 0:
                        raise ValueError("crop size is 0, what just happend?")
                    answers.append(classes["none"])
                    confidences.append(-1)
                    image_sequence.append((crop, True))
                    box_sequence.append(my_box)
                    bbox_sequence.append(selected_bbox)
                    if not from_tracker[-1]:
                        last_known_valid_bbox = selected_bbox.copy()
            if frame_count + 1 >= np.abs(cursor):
                # sets important variables to cursor location
                cur_frame = frames[cursor]
                cur_bbox = bbox_sequence[cursor]
                is_from_tracker = from_tracker[cursor]
            if len(image_sequence) == opt.sliding_window_size:
                # we have enough frames for prediction, predict for middle frame
                frames.pop(0)
                bbox_sequence.pop(0)
                from_tracker.pop(0)
                if image_sequence[opt.sliding_window_size // 2][1]:
                    # if middle image is valid
                    to_predict = {
                        "imgs": torch.tensor(
                            np.array([x[0] for x in image_sequence[0::2]]),
                            dtype=torch.float,
                        )
                        .squeeze()
                        .permute(0, 3, 1, 2)
                        .to(opt.device),
                        "boxs": torch.tensor(
                            np.array(box_sequence[::2]), dtype=torch.float
                        ).to(opt.device),
                    }
                    with torch.set_grad_enabled(False):
                        outputs = gaze_model(
                            to_predict
                        ).detach()  # actual gaze prediction
                        probs = torch.nn.functional.softmax(outputs, dim=1)
                        _, prediction = torch.max(outputs, 1)
                        confidence, _ = torch.max(probs, 1)
                        float32_conf = confidence.cpu().numpy()[0]
                        int32_pred = prediction.cpu().numpy()[0]
                    # update answers for the middle frame
                    answers[loc] = int32_pred
                    # update confidences for the middle frame
                    confidences[loc] = float32_conf
                image_sequence.pop(0)
                box_sequence.pop(0)

                if opt.illegal_transitions_path:
                    if len(answers) >= max_illegal_transition_length:
                        answers, confidences = fix_illegal_transitions(
                            loc,
                            answers,
                            confidences,
                            illegal_transitions,
                            corrected_transitions,
                        )
            # report results at cursor
            if frame_count + 1 >= np.abs(cursor):
                class_text = reverse_classes[answers[cursor]]
                if opt.mirror_annotation:
                    if class_text == "left":
                        class_text = "right"
                    elif class_text == "right":
                        class_text = "left"
                if opt.on_off:
                    class_text = "off" if class_text == "away" else "on"
                user_abort = handle_output(
                    opt,
                    is_from_tracker,
                    cur_frame,
                    cur_bbox,
                    confidences[cursor],
                    cursor,
                    class_text,
                    frame_count,
                    video_output_file,
                    prediction_output_file,
                    ui_output_components,
                    cur_fps,
                )
                if user_abort:
                    break
            ret_val, frame = cap.read()
            frame_count += 1
        if not user_abort:
            for i in range(
                opt.sliding_window_size - np.abs(cursor), opt.sliding_window_size - 1
            ):
                # report for final left over frames
                class_text = "none"
                cur_frame = frames[i]
                cur_bbox = bbox_sequence[i]
                is_from_tracker = from_tracker[i]
                user_abort = handle_output(
                    opt,
                    is_from_tracker,
                    cur_frame,
                    cur_bbox,
                    -1,
                    cursor,
                    class_text,
                    frame_count,
                    video_output_file,
                    prediction_output_file,
                    ui_output_components,
                    cur_fps,
                )
                frame_count = frame_count + 1
                if user_abort:
                    break
        # finished processing a video file, cleanup
        cleanup(
            video_output_file,
            prediction_output_file,
            answers,
            confidences,
            framerate,
            frame_count,
            cap,
            opt,
        )


def handle_output(
    opt,
    is_from_tracker,
    cur_frame,
    cur_bbox,
    confidence,
    cursor,
    class_text,
    frame_count,
    video_output_file,
    prediction_output_file,
    ui_output_components,
    cur_fps,
):
    # utility function to handle output (video, live stream, annotations, logging, etc.)
    if opt.output_video_path:
        if is_from_tracker and opt.track_face:
            rect_color = (0, 0, 255)
        else:
            rect_color = (0, 255, 0)
        draw.prepare_frame(
            cur_frame,
            cur_bbox,
            show_arrow=True,
            rect_color=rect_color,
            conf=confidence,
            class_text=class_text,
            frame_number=frame_count + cursor + 1,
            pic_in_pic=opt.pic_in_pic,
        )
        video_output_file.write(cur_frame)
    if opt.show_output:
        if is_from_tracker and opt.track_face:
            rect_color = (0, 0, 255)
        else:
            rect_color = (0, 255, 0)
        draw.prepare_frame(
            cur_frame,
            cur_bbox,
            show_arrow=True,
            rect_color=rect_color,
            conf=confidence,
            class_text=class_text,
            frame_number=frame_count + cursor + 1,
            pic_in_pic=opt.pic_in_pic,
        )

        cv2.imshow("frame", cur_frame)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            return True
    # handle writing output to file
    if opt.output_annotation:
        if opt.output_format == "raw_output":
            with open(prediction_output_file, "a", newline="") as f:
                f.write(
                    "{}, {}, {:.02f}\n".format(
                        str(frame_count + cursor + 1),
                        class_text,
                        confidence,
                    )
                )
        elif opt.output_format == "ui":
            if is_from_tracker and opt.track_face:
                rect_color = (0, 0, 255)
            else:
                rect_color = (0, 255, 0)
            output_for_ui = ui_packaging.prepare_frame_for_ui(
                cur_frame,
                cur_bbox,
                rect_color=rect_color,
                conf=confidence,
                class_text=class_text,
                frame_number=frame_count + cursor + 1,
                pic_in_pic=opt.pic_in_pic,
            )
            ui_packaging.save_ui_output(
                frame_idx=frame_count + cursor + 1,
                ui_output_components=ui_output_components,
                output_for_ui=output_for_ui,
            )
    logging.info(
        "frame: {}, class: {}, confidence: {:.02f}, cur_fps: {:.02f}".format(
            str(frame_count + cursor + 1),
            class_text,
            confidence,
            cur_fps(),
        )
    )
    return False


def cleanup(
    video_output_file,
    prediction_output_file,
    answers,
    confidences,
    framerate,
    frame_count,
    cap,
    opt,
):
    # saves and frees resources
    if opt.show_output:
        cv2.destroyAllWindows()
    if opt.output_video_path:
        video_output_file.release()
    if opt.output_annotation:  # write footer to file
        if opt.output_format == "compressed":
            answers = np.array(answers)
            confidences = np.array(confidences)
            if opt.mirror_annotation:
                lefts = answers == classes["left"]
                rights = answers == classes["right"]
                answers[lefts] = classes["right"]
                answers[rights] = classes["left"]
            np.savez(prediction_output_file, answers, confidences)
    cap.release()


def main():
    args = options.parse_arguments()
    if args.log:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            filename=args.log, filemode="w", level=args.verbosity.upper()
        )
    else:
        logging.basicConfig(level=args.verbosity.upper())
    if args.app:
        run_icatcher_app()
    else:
        predict_from_video(args)


if __name__ == "__main__":
    main()
